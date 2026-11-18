# Windy — production-like private deployment runbook (generic)

Deploy the existing single-container Windy app behind an authenticating TLS
reverse proxy. All paths/hosts below are placeholders — substitute the real
server values in a server-local copy of this document, never in git.

## 1. Source of truth

Deploy an exact verified commit of `main` (record the SHA). Build with the
committed `Dockerfile`; the image serves the built Vue UI and FastAPI on one
port (default 8000, loopback-bound via Compose).

## 2. Container

```bash
cp deploy/compose.prod.yml /srv/<app-dir>/compose.prod.yml   # server-local copy
cd /srv/<app-dir>
# edit compose.prod.yml: <WINDY_PORT> (free loopback port) and <WINDY_DATA_DIR>
git checkout <verified-SHA>                                   # or export the source tree
docker compose -f compose.prod.yml build
docker compose -f compose.prod.yml config                     # validate effective config
docker compose -f compose.prod.yml up -d
docker inspect windy-prod --format '{{.State.Health.Status}}' # expect: healthy
```

- Persistent data: `<WINDY_DATA_DIR>/windy.db` (SQLite, WAL) — host-owned
  directory, writable by the container user, readable by the backup operator.
- Exactly ONE scheduler instance runs inside the container
  (`WINDY_SCHEDULER_ENABLED=1`); verify no duplicate cron/systemd collectors.

## 3. TLS reverse proxy + authentication (nginx example)

```nginx
server {
    listen 80;
    server_name windy.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    http2 on;
    server_name windy.example.com;

    ssl_certificate     /etc/letsencrypt/live/windy.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/windy.example.com/privkey.pem;

    auth_basic "Windy";
    auth_basic_user_file /etc/nginx/windy.htpasswd;   # htpasswd -Bc /etc/nginx/windy.htpasswd <user>

    client_max_body_size 25m;                          # actuals upload (app limit 20 MB)
    proxy_read_timeout 120s;

    location / {
        proxy_pass http://127.0.0.1:<WINDY_PORT>;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

TLS issuance (Let's Encrypt, HTTP-01) AFTER the HTTP block exists:

```bash
certbot certonly --nginx -d windy.example.com
nginx -t && systemctl reload nginx
```

Verify: `curl -u user:pass https://windy.example.com/api/health` → 200;
without credentials → 401 on **GET and POST**; the container port answers only
on loopback (`ss -ltnp | grep <WINDY_PORT>`).

## 4. Update / rollback

```bash
# update: checkout new verified SHA, rebuild, recreate (DB volume untouched)
git checkout <new-SHA> && docker compose -f compose.prod.yml build && \
  docker compose -f compose.prod.yml up -d

# rollback: checkout previous SHA, rebuild, recreate — the DB volume persists
```

Migrations are additive and versioned (`schema_version`); a fresh DB seeds
itself, an existing DB migrates forward on startup. Never seed over existing
archive/actuals.

## 5. Backup / restore

```bash
deploy/backup.sh /srv/<data-dir>/windy.db /srv/<backups-dir>   # online backup API, keeps last 14
deploy/restore.sh /srv/<backups-dir>/windy-<STAMP>.db /tmp/windy-restore-check /srv/<data-dir>
```

Restore verifies integrity/schema on a disposable copy and prints the manual
promotion steps (app stop → file swap → app start). Backups stay on private
server storage.

## 6. Smoke checklist after deployment

- `https://...` valid TLS for the hostname; HTTP → HTTPS redirect.
- Unauthenticated GET/POST to `/`, `/api/health`, `/api/assets`, `/api/collect`,
  `/docs`, `/openapi.json` → 401; authenticated → works.
- SPA deep links (`/history?site=...`, `/availability?site=...`) survive refresh.
- One scheduler job registered; logs free of secrets/tracebacks.
- Live provider evidence: `python scripts/live_smoke.py --site <id> --out /tmp/evidence.json`.
