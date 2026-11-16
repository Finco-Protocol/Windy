"""SQLite persistence: WAL mode, versioned schema, tiny connection helper.

One connection per call (SQLite connections are cheap; avoids cross-thread
issues with FastAPI's threadpool). Generated `.db` files are never committed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  capacity_mw REAL NOT NULL,
  turbine_count INTEGER NOT NULL,
  turbine_model TEXT,
  turbine_rated_mw REAL,
  hub_height_m REAL NOT NULL,
  loss_factor REAL NOT NULL DEFAULT 0,
  timezone TEXT NOT NULL DEFAULT 'Europe/Podgorica',
  power_curve_id TEXT,
  assumptions_json TEXT NOT NULL DEFAULT '{}',
  notes TEXT
);

CREATE TABLE IF NOT EXISTS power_curves (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  provenance TEXT NOT NULL,
  rated_power_mw REAL NOT NULL,
  cut_in_mps REAL,
  rated_mps REAL,
  cut_out_mps REAL,
  points_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  source_provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  run_issued_utc TEXT,
  dedupe_key TEXT NOT NULL,
  issue_source TEXT NOT NULL,
  requested_height_m REAL NOT NULL,
  height_method TEXT NOT NULL,
  height_sources_json TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  source_url TEXT,
  forecast_days INTEGER,
  status TEXT NOT NULL,
  error TEXT,
  raw_json TEXT,
  UNIQUE (site_id, model_id, dedupe_key, requested_height_m)
);

CREATE TABLE IF NOT EXISTS forecast_points (
  run_id INTEGER NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
  valid_at_utc TEXT NOT NULL,
  wind_speed_mps REAL,
  wind_direction_deg REAL,
  height_method TEXT NOT NULL,
  modeled_power_mw REAL,
  interval_energy_mwh REAL,
  interval_hours REAL NOT NULL DEFAULT 1.0,
  status TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (run_id, valid_at_utc)
);

CREATE TABLE IF NOT EXISTS actual_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  filename TEXT NOT NULL,
  imported_at_utc TEXT NOT NULL,
  timezone TEXT NOT NULL,
  mapping_json TEXT NOT NULL,
  rows_total INTEGER NOT NULL,
  rows_ok INTEGER NOT NULL,
  warnings_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS actual_points (
  site_id TEXT NOT NULL REFERENCES assets(id),
  interval_start_utc TEXT NOT NULL,
  interval_end_utc TEXT NOT NULL,
  energy_mwh REAL,
  power_mw REAL,
  import_id INTEGER NOT NULL REFERENCES actual_imports(id),
  PRIMARY KEY (site_id, interval_start_utc)
);

CREATE INDEX IF NOT EXISTS idx_runs_site_model ON forecast_runs(site_id, model_id, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_points_valid ON forecast_points(valid_at_utc);
CREATE INDEX IF NOT EXISTS idx_actual_valid ON actual_points(site_id, interval_start_utc);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with WAL, FK enforcement and a busy timeout."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    """Create schema if missing and stamp the schema version."""
    with session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
