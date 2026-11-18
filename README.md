# Windy

Personal project. A small local web application: a Vue 3 frontend (adapted from
[MeteoCompare](https://github.com/Flowm/meteocompare)) served by a FastAPI backend with
SQLite storage. Intended for local, single-user use.

## Local development

Backend (Python 3.12+):

```bash
cd backend
python -m pip install -r requirements.txt
python -m app.cli init-db
uvicorn app.main:app --port 8000
```

Frontend (Node 20+, pnpm via corepack):

```bash
cd frontend
corepack pnpm install
corepack pnpm dev          # http://localhost:5173, proxies /api to :8000
```

Full local stack (builds the frontend and serves it from the backend):

```bash
docker compose up --build  # http://127.0.0.1:8000
```

## Forecast modes (v1.8)

**Default — Per turbine (26):** the forecast screen requests a separate,
read-only archived 26-location NWP projection. On **Osvježi 26 turbina** the
backend first collects the original one-spot run, then obtains a *single*
multi-coordinate Open-Meteo Single Runs response per model, for all 26 WTG
GPS positions. Spatial snapshots are stored separately and immutably by
issue/run, location fingerprint and inventory revision (additive SQLite v6).
GET requests never invoke Open-Meteo. Previously archived one-spot runs,
actuals and historical accuracy stay untouched.

**Alternative — 1 spot:** selects the original central-point calculation and
all its prior charts, Excel exports, operations, archive and history. Spatial
mode uses its own 26-WTG curve/rated/availability aggregation for the SAME
existing charts, Compare and hourly ensemble; an absent spatial snapshot never
silently falls back to 1-spot wind/MWh. Both modes support 7/10/15-day display,
MWh/GWh and mode-correct Excel export.

Spatial predictions are model-grid estimates, **not** 26 independent
anemometer measurements, and **not** a CFD/wake-loss or calibrated complex
terrain model. The owner-provided 26 coordinates and elevations have
**provisional WTG-ID mapping** until validated against as-built IDs. The
per-unit reference curve must be physically compatible with its nameplate,
or a **separate explicit scaled-reference opt-in** is required; incompatible
curves result in missing forecasts (never fabricated energy). Missing any WTG
wind hour makes the whole farm-hour unavailable rather than treating that WTG
as zero. Farm export caps apply after per-unit ON/OFF and curtailments. The
per-unit adjusted Excel columns are pre-farm-export-cap.

Collection is an **explicit manual** action; the default scheduler remains
spot-only to prevent unapproved 26-coordinate recurrent provider use. Open-Meteo
free endpoints are for **non-commercial use**; check licensing/usage quotas
before enabling routine commercial collection. Do not publish the unauthenticated
write endpoints without an authenticating proxy or VPN.

Read-only API: `GET /api/turbine-forecast?site_id=krnovo&days=7` and
`GET /api/forecast?site_id=krnovo&days=7&modeling_mode=per_turbine`.
Explicit refresh: `POST /api/collect/turbines` with `{"site_id":"krnovo","days":16}`.
The existing `/api/forecast` with no modelling-mode parameter remains the
backward-compatible `spot` default for external API consumers.

## Tests

```bash
cd backend && python -m pytest -q
cd frontend && corepack pnpm vitest run && corepack pnpm type-check && corepack pnpm build
```

CI runs the same two suites on every PR and push to `main`.

## Structure

- `backend/` — FastAPI application, SQLite storage, CLI and scheduler
- `frontend/` — Vue 3 + TypeScript + Vite application
- `.github/workflows/ci.yml` — CI

## Attribution and licenses

- Frontend adapted from [Flowm/meteocompare](https://github.com/Flowm/meteocompare) (MIT) —
  see `frontend/README.md` and `NOTICE.md`
- [wind-python/windpowerlib](https://github.com/wind-python/windpowerlib) (MIT) used as a library
- [Devs-Hunting/enercast](https://github.com/Devs-Hunting/enercast) (MIT) reviewed as a
  backend design reference
- Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0)

Private data (databases, uploaded files, secrets) never belongs in git — see `.gitignore`
and `.env.example`.
