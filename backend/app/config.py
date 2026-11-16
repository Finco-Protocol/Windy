"""Windy backend — environment-driven configuration.

All defaults are local-file friendly: the SQLite database lives under
`<repo>/data/` (gitignored) so it survives restarts and can be mounted as a
volume in Docker.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
REPO_DIR = BACKEND_DIR.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Process-wide settings; instantiated once in app.main.create_app."""

    def __init__(self) -> None:
        data_dir_default = REPO_DIR / "data"
        self.data_dir = _env_path("WINDY_DATA_DIR", data_dir_default)
        self.db_path = _env_path("WINDY_DB_PATH", self.data_dir / "windy.db")
        self.scheduler_enabled = _env_flag("WINDY_SCHEDULER_ENABLED", "0")
        # Twice-daily default schedule, 00:00 and 12:00 UTC (5-field cron).
        self.schedule_cron = os.environ.get("WINDY_SCHEDULE_CRON", "0 0,12 * * *")
        self.om_timeout_s = float(os.environ.get("WINDY_OM_TIMEOUT_S", "40"))
        self.om_max_retries = int(os.environ.get("WINDY_OM_MAX_RETRIES", "3"))
        cors = os.environ.get("WINDY_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        self.cors_origins = [o.strip() for o in cors.split(",") if o.strip()]
        # Built frontend directory served by the backend (Docker image). In dev
        # (`vite dev`) this does not exist and the API-only backend is correct.
        dist_default = REPO_DIR / "frontend" / "dist"
        self.frontend_dist = _env_path("WINDY_FRONTEND_DIST", dist_default)
        # Default forecast collection depth (days); clamped per-model in registry.
        self.collect_days = int(os.environ.get("WINDY_COLLECT_DAYS", "10"))
        # Site the scheduled (twice-daily) collection runs for. Deliberately a
        # single explicit site so newly created sites can never flood the
        # provider from the background scheduler; collect others manually.
        self.collect_site_id = os.environ.get("WINDY_COLLECT_SITE_ID", "krnovo")
