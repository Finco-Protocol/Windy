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

SCHEMA_VERSION = 5

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
  notes TEXT,
  allow_scaled_reference INTEGER NOT NULL DEFAULT 0
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
  points_json TEXT NOT NULL,
  site_id TEXT
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

# --- versioned migrations -------------------------------------------------------
# Each migration runs once, in order, on existing databases; SCHEMA_SQL above
# already contains the v2 tables so fresh databases create them directly.
# Migrations are additive only — never destructive, never reset user data.

MIGRATION_2_SQL = """
CREATE TABLE IF NOT EXISTS backfill_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  params_json TEXT NOT NULL,
  planned_runs_json TEXT NOT NULL,
  status TEXT NOT NULL,
  cursor_index INTEGER NOT NULL DEFAULT 0,
  n_stored INTEGER NOT NULL DEFAULT 0,
  n_already INTEGER NOT NULL DEFAULT 0,
  n_unavailable INTEGER NOT NULL DEFAULT 0,
  n_failed INTEGER NOT NULL DEFAULT 0,
  n_skipped INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  site_id TEXT NOT NULL DEFAULT 'krnovo'
);

CREATE TABLE IF NOT EXISTS backfill_results (
  job_id INTEGER NOT NULL REFERENCES backfill_jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  run_issued_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS calibrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  factor REAL NOT NULL,
  n_train_hours INTEGER NOT NULL,
  n_val_hours INTEGER NOT NULL,
  val_mae_before REAL,
  val_mae_after REAL,
  val_wape_before REAL,
  val_wape_after REAL,
  trained_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  UNIQUE (site_id, model_id)
);

CREATE TABLE IF NOT EXISTS turbines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  unit_id TEXT NOT NULL,
  name TEXT,
  rated_mw REAL NOT NULL,
  curve_id TEXT,
  config_revision INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  UNIQUE (site_id, unit_id, config_revision)
);

CREATE TABLE IF NOT EXISTS operational_constraints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  kind TEXT NOT NULL,
  unit_ids_json TEXT,
  cap_mw REAL,
  cap_percent REAL,
  start_utc TEXT NOT NULL,
  end_utc TEXT,
  revision INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'owner',
  superseded_by INTEGER
);

CREATE TABLE IF NOT EXISTS scenario_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES forecast_runs(id),
  inventory_revision INTEGER NOT NULL,
  constraints_revision INTEGER NOT NULL,
  algorithm_version TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  summary_json TEXT,
  UNIQUE (site_id, run_id, inventory_revision, constraints_revision)
);

CREATE INDEX IF NOT EXISTS idx_turbines_rev ON turbines(site_id, config_revision);
CREATE INDEX IF NOT EXISTS idx_constraints_rev ON operational_constraints(site_id, revision);
"""


MIGRATION_3_SQL = """
-- v3: multi-site — every batch job belongs to a site; a curve may be marked
-- site-specific (NULL = generic reference curve). Additive only.
ALTER TABLE backfill_jobs ADD COLUMN site_id TEXT NOT NULL DEFAULT 'krnovo';
ALTER TABLE power_curves ADD COLUMN site_id TEXT;
"""

MIGRATION_4_SQL = """
-- v4: turbine inventory, time-dependent operational constraints and published
-- scenario revisions. Additive only; existing rows are untouched.
CREATE TABLE IF NOT EXISTS turbines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  unit_id TEXT NOT NULL,
  name TEXT,
  rated_mw REAL NOT NULL,
  curve_id TEXT,
  config_revision INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  UNIQUE (site_id, unit_id, config_revision)
);
CREATE TABLE IF NOT EXISTS operational_constraints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  kind TEXT NOT NULL,
  unit_ids_json TEXT,
  cap_mw REAL,
  cap_percent REAL,
  start_utc TEXT NOT NULL,
  end_utc TEXT,
  revision INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'owner',
  superseded_by INTEGER
);
CREATE TABLE IF NOT EXISTS scenario_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES forecast_runs(id),
  inventory_revision INTEGER NOT NULL,
  constraints_revision INTEGER NOT NULL,
  algorithm_version TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  summary_json TEXT,
  UNIQUE (site_id, run_id, inventory_revision, constraints_revision)
);
CREATE INDEX IF NOT EXISTS idx_turbines_rev ON turbines(site_id, config_revision);
CREATE INDEX IF NOT EXISTS idx_constraints_rev ON operational_constraints(site_id, revision);
"""


MIGRATION_5_SQL = """
-- v5 (PR#8 Correction A): append-only site configuration revisions (same-site
-- versioned edit workflow — never an in-place silent rewrite) and append-only
-- per-turbine location records (lat/lon/elevation with provenance). Additive
-- only; existing rows are untouched. No seed data is written by migrations.
CREATE TABLE IF NOT EXISTS site_config_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  revision INTEGER NOT NULL,
  changes_json TEXT NOT NULL,
  effective_from_utc TEXT NOT NULL,
  note TEXT,
  created_at_utc TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'owner',
  UNIQUE (site_id, revision)
);
CREATE TABLE IF NOT EXISTS turbine_locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id TEXT NOT NULL REFERENCES assets(id),
  revision INTEGER NOT NULL,
  unit_id TEXT NOT NULL,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  elevation_m REAL,
  source TEXT NOT NULL DEFAULT 'owner',
  created_at_utc TEXT NOT NULL,
  UNIQUE (site_id, revision, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_config_rev ON site_config_revisions(site_id, revision);
CREATE INDEX IF NOT EXISTS idx_locations_rev ON turbine_locations(site_id, revision);
"""

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations in order. SCHEMA_SQL is idempotent (IF NOT
    EXISTS) and covers fresh databases; the version stamp gates re-runs."""
    current = 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row:
        try:
            current = int(row["value"])
        except ValueError:
            current = 0
    if current < SCHEMA_VERSION:
        if current < 2:
            conn.executescript(MIGRATION_2_SQL)
        if current < 3:
            # ALTER TABLE ... ADD COLUMN fails if the column already exists;
            # guard per column so partially-migrated databases converge.
            if "site_id" not in _columns(conn, "backfill_jobs"):
                conn.execute("ALTER TABLE backfill_jobs ADD COLUMN site_id TEXT NOT NULL DEFAULT 'krnovo'")
            if "site_id" not in _columns(conn, "power_curves"):
                conn.execute("ALTER TABLE power_curves ADD COLUMN site_id TEXT")
        if current < 4:
            conn.executescript(MIGRATION_4_SQL)
            if "allow_scaled_reference" not in _columns(conn, "assets"):
                conn.execute("ALTER TABLE assets ADD COLUMN allow_scaled_reference INTEGER NOT NULL DEFAULT 0")
        if current < 5:
            conn.executescript(MIGRATION_5_SQL)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


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
    """Create schema if missing, apply pending migrations, stamp the version."""
    with session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
