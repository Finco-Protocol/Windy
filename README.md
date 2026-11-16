# Windy

Private-use **wind farm forecast & forecast-accuracy application**.
Initial asset: **Krnovo Wind Farm, Montenegro** (26 × 2.75 MW = 71.5 MW, hub 90 m *provisional*).

Windy fetches multi-model wind forecasts (ECMWF IFS, NOAA GFS, DWD ICON — via Open-Meteo),
converts them to estimated site power, **archives every forecast issue/run unchanged**, lets you
retrieve historical issues, import actual generation from CSV/XLSX, and compare forecasts against
actuals (MAE, bias, WAPE per lead-time bin). It runs locally and on a simple VPS.

> **Status: reference tool, not an operational forecast.** The power model is a
> manufacturer-class but *uncalibrated* curve; MWh numbers are estimates. See
> [Limitations](#limitations).

---

## The three screens

| Screen | Route | What it does |
| --- | --- | --- |
| **Forecast** | `/` | Latest archived run per model: hourly hub-height wind (m/s), modeled power (MW), daily MWh (site-local days), cumulative MWh, per-model issue time / height method / coverage / warnings, 7-day / 10-day toggle, `Fetch now`. |
| **Historical runs** | `/history` | Every archived issue cycle (immutable rows). Select a run and see what it predicted for a target date. Bounded backfill of **one** archived run per action. Archive CSV download. |
| **Actuals & accuracy** | `/actuals` | CSV/XLSX import with column mapping and DST-safe UTC normalization, import preview/errors/warnings, actual-vs-archived-forecast aligned chart, accuracy table (MAE/bias MW, WAPE energy, coverage) per model × lead bin. |

Data-integrity rules enforced in code (also visible in the UI):

- Forecasts are archived per **issue cycle** — yesterday's forecast is never overwritten by
  today's; re-collecting the same cycle is an idempotent upsert.
- Issue times come from the **Open-Meteo Single Runs archive** (`&run=`). The live-forecast
  fallback is stored with **issue time unknown** and can never be scored against actuals.
- Hub wind (90 m) is resolved per model from its documented levels: GFS **log-interp 80+100 m**,
  ICON **log-interp 80+120 m**, ECMWF **100 m proxy**. Missing levels stay `missing-height`
  (null) — never interpolated from 10 m, never zero-filled.
- ICON-EU stops at 120 h, ICON Global around 180 h — the UI shows exactly the available horizon
  and cross-model totals only over the **common valid-time intersection**.
- Power: GE103/2750 curve (the GE 2.75-103 class, from windpowerlib's bundled OEDB turbine
  library) × 26 turbines, hard-capped at 71.5 MW, zero below cut-in (3 m/s) and above cut-out
  (25 m/s), no silent density/loss double-counting (loss factor default 0).
- Accuracy pairs only **archived runs with a known issue time** against actuals of the *same
  valid time*, with `valid_at > run_issued` (no leakage).

## Quick start (local, no Docker)

Requirements: Python 3.12+, Node 20+ (pnpm via corepack).

```bash
# 1) backend
cd backend
python -m pip install -r requirements.txt
python -m app.cli init-db                      # schema + Krnovo asset + curve
uvicorn app.main:app --port 8000               # API on http://127.0.0.1:8000

# 2) first data collection (real Open-Meteo fetch, ~20-60 s)
python -m app.cli collect --days 10

# 3) frontend (dev mode, second terminal)
cd frontend
corepack pnpm install
corepack pnpm dev                              # http://localhost:5173 (proxies /api → :8000)
```

**One-command production-ish start** (builds the frontend and serves it from the backend):

```bash
docker compose up --build      # app on http://127.0.0.1:8000, DB persisted in ./data/
```

The backend serves the built SPA including history-mode deep links (`/history`, `/actuals` on
hard refresh); missing asset chunks stay real 404s. The compose file binds to `127.0.0.1` only —
the app is single-user and unauthenticated, so put an authenticating reverse proxy or VPN in
front before ever widening that binding.

## Scheduling (twice daily)

Three equivalent options:

1. **Built-in scheduler** — set `WINDY_SCHEDULER_ENABLED=1` (docker-compose does this); runs at
   `WINDY_SCHEDULE_CRON` (default `0 0,12 * * *` UTC, i.e. 00:00 and 12:00).
2. **CLI from cron/systemd timer** — `python -m app.cli collect` (idempotent; a re-run of the
   same cycle does not duplicate rows).
3. **Manual** — the `Fetch now` button on the Forecast screen.

Collection probes the Single Runs archive backwards from the newest 6-hourly cycle (models
publish 00/06/12/18 UTC runs with publication latency), so a scheduled collect stores the newest
run that actually exists, with its true issue time.

## Importing actual generation

On *Actuals & accuracy* → *Import actual generation*:

- Upload **CSV** (also XLSX). Required mapping: timestamp column, value column, and whether the
  value is **energy MWh per interval** or **interval-average MW** (conversion uses the interval
  duration; empty interval = inferred from the data).
- Timestamps: ISO with offset/Z, or naive local (default tz `Europe/Podgorica`, editable).
  DST spring-forward gaps are **errors**; fall-back ambiguity is **warned** and interpreted as
  the first occurrence. Everything is persisted in UTC.
- `Preview` shows parsed rows, row-level errors, decimal-comma handling, duplicates inside the
  file, and conflicts with already-stored rows. Overwriting existing actuals requires the explicit
  **replace** choice (default is reject). A file with invalid rows is **blocked** from import
  (never partially imported) until the listed rows are fixed.
- Accuracy appears once archived forecast issues overlap imported actuals; until then the screen
  says *Actuals not imported / accuracy not available* — no invented numbers.

## Where data lives

- SQLite at `WINDY_DB_PATH` (default `./data/windy.db`, WAL mode). Tables: `assets`,
  `power_curves`, `forecast_runs` (+ raw API payload per run), `forecast_points`,
  `actual_imports`, `actual_points`. Generated DB files are never committed.
- `data/` is gitignored; in Docker it is the mounted `/data` volume.

## API overview (all under `/api`)

`GET /health`, `GET /site`, `POST /collect`, `GET /forecast?days=`, `GET /runs`, `GET /runs/{id}`,
`GET /compare?run_id&target_date`, `POST /backfill` (one run per call), `POST /actuals/preview`,
`POST /actuals/import`, `GET /actuals`, `GET /accuracy?from_utc&to_utc`,
`GET /accuracy/overlay?from_utc&to_utc&lead_bin=`, `GET /export/forecast.csv`, `GET /export/actuals.csv`.

## Reuse — what came from upstream

| Source | Commit (cloned 2026-11-15) | What Windy reused |
| --- | --- | --- |
| [Flowm/meteocompare](https://github.com/Flowm/meteocompare) (MIT) | `9d6bb7bf7ad33c416229d8afc02447d43b540250` | **The entire frontend** (`frontend/`): Vue 3 + TS + Vite + Tailwind + ECharts app, `src/api/openMeteo.ts` (429 retry/backoff), `src/api/omSingleRuns.ts` (Single Runs adapter + missing-model attribution — ported to Python in `backend/app/openmeteo.py`), model registry (trimmed in `backend/app/registry.py`), UI primitives (`SegmentedToggle`, `StateBlock`, `CollapsibleSection`, `AppFooter`, chart theme), service-worker/PWA setup, 456 upstream unit tests (all still passing). Windy replaced the weather pages with its own three screens (`src/views/Windy*.vue`, `src/api/windy.ts`), removed the Cloudflare `worker/`, PostHog telemetry, and the geocoding-driven location flow (fixed Krnovo asset served by the backend). |
| [Devs-Hunting/enercast](https://github.com/Devs-Hunting/enercast) (MIT) | `695e24835208430314f24af6bae45c6d590bc534` | Reviewed as the backend reference; Windy adopted its fetch→validate→persist client pattern and typed API-error taxonomy conceptually. No code transplanted (its domain is Polish grid-level ENTSO-E data with a Streamlit UI). |
| [wind-python/windpowerlib](https://github.com/wind-python/windpowerlib) (MIT) | `191d8ca894d6d830997630fa3599f048d6e8c7b4` | Used as a **library**: `WindTurbine(turbine_type='GE103/2750')` provides the manufacturer-class GE 2.75-103 power curve from its bundled local OEDB turbine-library snapshot (`backend/app/power.py`, 51 points, cut-in 3 m/s, cut-out 25 m/s). Farm aggregation/cap logic is Windy's own. |
| [pratham94380/wind-forecast-app](https://github.com/pratham94380/wind-forecast-app) | — | Not used (see delivery report). |

Open-Meteo is the single data provider; ECMWF/NOAA/DWD are the underlying model origins, **not**
independent provider APIs. Public endpoints only; no API key needed (upstream's commercial-host
`customer-` switch is preserved in the frontend for a paid key if ever needed).

## Verified during this build (2026-11-15)

- Live Open-Meteo multi-model forecast for Krnovo returned values at 10/80/100/120 m for all four
  models (180 m null for ECMWF/GFS) — HTTP 200.
- Live Single Runs archive probe: ECMWF IFS run `2026-11-14T00:00` for Krnovo, 72 rows, 0 nulls — HTTP 200.
- Live `collect` via the app: ECMWF run `2026-11-15T12:00` (240 pts), ICON Global `12:00` (192 pts),
  ICON-EU `18:00` (120 pts); GFS archive had no run yet → honest `ok-issue-unknown` fallback.
- Stored values spot-checked byte-for-byte against a fresh raw API response (GFS 80 m avg 3.43 m/s both).
- All three screens rendered in a browser against the live backend (screenshots in the PR).

## Tests

```bash
# backend (56 fast, deterministic tests; fixtures are synthetic)
cd backend && python -m pytest -q

# frontend (upstream 456 + 4 new windy tests)
cd frontend && corepack pnpm vitest run
corepack pnpm type-check      # vue-tsc, strict
corepack pnpm build           # production build + PWA
```

A small root CI (`.github/workflows/ci.yml`) runs the same two suites on every PR to `main` and
push to `main` — backend pytest + frontend install/vitest/type-check/build, two jobs, no deploys.

## Limitations

- **Uncalibrated power model** — GE103/2750 is the right turbine *class* but site density, wake,
  availability and blade-condition losses are not modelled (loss factor 0 default). Treat MWh as
  reference estimates until the curve/losses are calibrated against measured output.
- GFS `gfs_seamless` runs were not yet in the Single Runs archive at build time; Windy handles
  that transparently (issue-unknown runs, excluded from scoring) rather than faking issue times.
- Hub height 90 m is provisional and editable; one representative coordinate is used for all queries.
- Single-user, no authentication — keep the port private (VPN/firewall) or front it with an
  authenticating proxy before any public exposure.

## Next 3 functional steps

1. Calibrate the power curve/loss factor once real actuals accumulate (compare & fit per wind bin).
2. Add a second asset (sites are already parameterized; needs a small Assets UI).
3. Extend backfill UI with batch ranges (server stays one-run-per-call).
