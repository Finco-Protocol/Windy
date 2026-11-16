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
