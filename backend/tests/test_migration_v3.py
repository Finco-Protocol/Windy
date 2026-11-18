"""Migration test: a genuine v1.1-shaped database upgrades to v3 in place,
preserving every row, raw payload, issue time and stored result."""

import sqlite3

from app.store import ensure_db

V1_1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT, latitude REAL NOT NULL,
  longitude REAL NOT NULL, capacity_mw REAL NOT NULL, turbine_count INTEGER NOT NULL,
  turbine_model TEXT, turbine_rated_mw REAL, hub_height_m REAL NOT NULL,
  loss_factor REAL NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT 'Europe/Podgorica',
  power_curve_id TEXT, assumptions_json TEXT NOT NULL DEFAULT '{}', notes TEXT
);
CREATE TABLE power_curves (
  id TEXT PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
  provenance TEXT NOT NULL, rated_power_mw REAL NOT NULL, cut_in_mps REAL, rated_mps REAL,
  cut_out_mps REAL, points_json TEXT NOT NULL
);
CREATE TABLE forecast_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, source_provider TEXT NOT NULL,
  model_id TEXT NOT NULL, run_issued_utc TEXT, dedupe_key TEXT NOT NULL, issue_source TEXT NOT NULL,
  requested_height_m REAL NOT NULL, height_method TEXT NOT NULL, height_sources_json TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL, source_url TEXT, forecast_days INTEGER, status TEXT NOT NULL,
  error TEXT, raw_json TEXT,
  UNIQUE (site_id, model_id, dedupe_key, requested_height_m)
);
CREATE TABLE forecast_points (
  run_id INTEGER NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
  valid_at_utc TEXT NOT NULL, wind_speed_mps REAL, wind_direction_deg REAL,
  height_method TEXT NOT NULL, modeled_power_mw REAL, interval_energy_mwh REAL,
  interval_hours REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (run_id, valid_at_utc)
);
CREATE TABLE actual_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, filename TEXT NOT NULL,
  imported_at_utc TEXT NOT NULL, timezone TEXT NOT NULL, mapping_json TEXT NOT NULL,
  rows_total INTEGER NOT NULL, rows_ok INTEGER NOT NULL, warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE actual_points (
  site_id TEXT NOT NULL, interval_start_utc TEXT NOT NULL, interval_end_utc TEXT NOT NULL,
  energy_mwh REAL, power_mw REAL, import_id INTEGER NOT NULL REFERENCES actual_imports(id),
  PRIMARY KEY (site_id, interval_start_utc)
);
CREATE TABLE backfill_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_utc TEXT NOT NULL, params_json TEXT NOT NULL,
  planned_runs_json TEXT NOT NULL, status TEXT NOT NULL, cursor_index INTEGER NOT NULL DEFAULT 0,
  n_stored INTEGER NOT NULL DEFAULT 0, n_already INTEGER NOT NULL DEFAULT 0,
  n_unavailable INTEGER NOT NULL DEFAULT 0, n_failed INTEGER NOT NULL DEFAULT 0,
  n_skipped INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE TABLE backfill_results (
  job_id INTEGER NOT NULL REFERENCES backfill_jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, model_id TEXT NOT NULL, run_issued_utc TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT, PRIMARY KEY (job_id, seq)
);
CREATE TABLE calibrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, model_id TEXT NOT NULL,
  factor REAL NOT NULL, n_train_hours INTEGER NOT NULL, n_val_hours INTEGER NOT NULL,
  val_mae_before REAL, val_mae_after REAL, val_wape_before REAL, val_wape_after REAL,
  trained_at_utc TEXT NOT NULL, status TEXT NOT NULL, note TEXT,
  UNIQUE (site_id, model_id)
);
INSERT INTO meta VALUES ('schema_version', '2');
"""


def _make_v11_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_1_SCHEMA)
    conn.execute(
        "INSERT INTO assets VALUES ('krnovo','K','ME',42.8756,19.1106,71.5,26,'GE',2.75,90.0,0.0,"
        "'Europe/Podgorica','c1','{}','n')"
    )
    conn.execute("INSERT INTO power_curves VALUES ('c1','C','manufacturer-reference','s','p',2.78,3,12,25,'[]')")
    conn.execute(
        "INSERT INTO forecast_runs (site_id, source_provider, model_id, run_issued_utc, dedupe_key, issue_source, "
        "requested_height_m, height_method, height_sources_json, fetched_at_utc, source_url, forecast_days, status, error, raw_json) "
        "VALUES ('krnovo','open-meteo','gfs_seamless','2026-11-10T00:00:00Z','2026-11-10T00:00:00Z',"
        "'single-runs-archive',90.0,'log-interp','[80,100]','2026-11-10T01:00:00Z','u',10,'ok',NULL,'{\"v\":1}')"
    )
    conn.execute(
        "INSERT INTO forecast_points VALUES (1,'2026-11-10T00:00:00Z',6.1,270,'log-interp',10.0,10.0,1.0,'ok')"
    )
    conn.execute("INSERT INTO actual_imports (site_id, filename, imported_at_utc, timezone, mapping_json, rows_total, rows_ok) "
                 "VALUES ('krnovo','a.csv','2026-11-10T02:00:00Z','UTC','{}',1,1)")
    conn.execute("INSERT INTO actual_points VALUES ('krnovo','2026-11-10T00:00:00Z','2026-11-10T01:00:00Z',5.0,5.0,1)")
    conn.execute("INSERT INTO backfill_jobs (created_at_utc, params_json, planned_runs_json, status) "
                 "VALUES ('2026-11-10T00:00:00Z','{}','[]','completed')")
    conn.execute("INSERT INTO backfill_results VALUES (1,0,'gfs_seamless','2026-11-10T00:00:00Z','stored',NULL)")
    conn.execute(
        "INSERT INTO calibrations (site_id, model_id, factor, n_train_hours, n_val_hours, trained_at_utc, status, note) "
        "VALUES ('krnovo','gfs_seamless',1.1,300,80,'2026-11-11T00:00:00Z','calibrated','old note')"
    )
    conn.commit()
    conn.close()


def test_v11_database_migrates_to_v3_non_destructively(tmp_path):
    db = tmp_path / "v11.db"
    _make_v11_db(db)

    ensure_db(db)  # migrate

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    assert version == "6"  # one run carries the DB through every pending migration
    # new columns exist with backward-compatible defaults
    assert conn.execute("SELECT site_id FROM backfill_jobs WHERE id=1").fetchone()["site_id"] == "krnovo"
    assert conn.execute("SELECT site_id FROM power_curves WHERE id='c1'").fetchone()["site_id"] is None
    # every pre-existing row and payload preserved exactly
    assert conn.execute("SELECT raw_json FROM forecast_runs WHERE id=1").fetchone()["raw_json"] == '{"v":1}'
    assert conn.execute("SELECT run_issued_utc FROM forecast_runs WHERE id=1").fetchone()["run_issued_utc"] == "2026-11-10T00:00:00Z"
    assert conn.execute("SELECT COUNT(*) c FROM forecast_points").fetchone()["c"] == 1
    assert conn.execute("SELECT energy_mwh FROM actual_points").fetchone()["energy_mwh"] == 5.0
    assert conn.execute("SELECT COUNT(*) c FROM actual_imports").fetchone()["c"] == 1
    assert conn.execute("SELECT status FROM backfill_jobs WHERE id=1").fetchone()["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) c FROM backfill_results").fetchone()["c"] == 1
    cal = conn.execute("SELECT factor, note FROM calibrations").fetchone()
    assert cal["factor"] == 1.1 and cal["note"] == "old note"
    # seeding did not duplicate the existing asset (it exists) or reset its curve
    assert conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"] == 1
    assert conn.execute("SELECT power_curve_id FROM assets WHERE id='krnovo'").fetchone()["power_curve_id"] == "c1"
    conn.close()

    # double migration is a no-op
    ensure_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "6"
    conn.close()
