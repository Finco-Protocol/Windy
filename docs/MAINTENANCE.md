# Maintenance

## Backup and restore (SQLite, local)

The app stores everything in one SQLite database (WAL mode). Default locations:

- plain run: `backend/../data/windy.db` — i.e. `<repo>/data/windy.db`
- Docker Compose: the `./data` directory mounted at `/data` in the container
  (`WINDY_DB_PATH=/data/windy.db`)

### Backup

Stop or quiesce the service first (the SQLite file is safe to copy when no
write is in flight; WAL mode keeps the file consistent for readers). Copy the
database and its WAL sidecars to a **temporary directory outside git**:

```bash
# plain run
cp data/windy.db /tmp/windy-backup.db        # plus data/windy.db-wal / -shm if present

# docker compose
docker compose exec windy cp /data/windy.db /tmp/windy-backup.db
docker compose cp windy:/tmp/windy-backup.db ./windy-backup.db
```

`windy-backup.db` must never be committed (`data/` and `*.db` are gitignored).

### Restore

Stop the app, replace the live file with the backup, restart:

```bash
cp /tmp/windy-backup.db data/windy.db
rm -f data/windy.db-wal data/windy.db-shm   # stale sidecars from the old file
docker compose up -d                         # or: uvicorn app.main:app --port 8000
```

Verified behavior: a restored copy boots with `schema_version` intact, keeps
all forecast runs, actuals, turbines, constraints and published scenario
revisions, and repeated migrations are a no-op (covered by
`backend/tests/test_migration_v4.py` and `test_e2e_journey.py` step G).

## Live provider smoke (REAL network evidence)

```bash
# REAL calls — not part of CI; capped at five public-endpoint requests
python scripts/live_smoke.py --site krnovo --out /tmp/live_evidence.json
```

Prints/saves JSON evidence: endpoint, HTTP status, model origin, valid
interval count, first/last valid UTC, horizon flag, issue-known provenance
for the archived Single Runs issuance.

## Scheduled collection

`WINDY_SCHEDULER_ENABLED=1` collects the single explicit site
`WINDY_COLLECT_SITE_ID` (default `krnovo`) at `WINDY_SCHEDULE_CRON`
(00:00/12:00 UTC). New sites are never auto-collected by the background
scheduler — collect them manually (`POST /api/collect {"site_id": ...}`) or
change the env deliberately.
