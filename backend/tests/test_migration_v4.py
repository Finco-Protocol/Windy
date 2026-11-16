"""V3→V4 migration: a representative schema-v3 database (runs, imports,
backfills, calibrations) upgrades to v4 preserving every row and payload."""

import json
import sqlite3

from app.store import ensure_db

from tests.test_migration_v3 import V1_1_SCHEMA  # same shape, stamped v3


def _make_v3_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_1_SCHEMA)
    # v3 additions
    conn.execute("ALTER TABLE backfill_jobs ADD COLUMN site_id TEXT NOT NULL DEFAULT 'krnovo'")
    conn.execute("ALTER TABLE power_curves ADD COLUMN site_id TEXT")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '3')")
    # realistic content
    conn.execute(
        "INSERT INTO assets VALUES ('krnovo','K','ME',42.8756,19.1106,71.5,26,'GE',2.75,90.0,0.0,"
        "'Europe/Podgorica','c1','{}','n')"
    )
    conn.execute("INSERT INTO power_curves VALUES ('c1','C','manufacturer-reference','s','p',2.78,3,12,25,'[]',NULL)")
    conn.execute(
        "INSERT INTO forecast_runs (site_id, source_provider, model_id, run_issued_utc, dedupe_key, issue_source, "
        "requested_height_m, height_method, height_sources_json, fetched_at_utc, source_url, forecast_days, status, error, raw_json) "
        "VALUES ('krnovo','open-meteo','ecmwf_ifs','2026-11-15T12:00:00Z','2026-11-15T12:00:00Z',"
        "'single-runs-archive',90.0,'proxy','[100]','2026-11-15T13:00:00Z','u',10,'ok',NULL,'{\"payload\":true}')"
    )
    conn.execute(
        "INSERT INTO forecast_points VALUES (1,'2026-11-15T12:00:00Z',5.5,240,'proxy',12.5,12.5,1.0,'proxy')"
    )
    conn.execute("INSERT INTO actual_imports (site_id, filename, imported_at_utc, timezone, mapping_json, rows_total, rows_ok) "
                 "VALUES ('krnovo','m.csv','2026-11-15T14:00:00Z','UTC','{}',2,2)")
    conn.execute("INSERT INTO actual_points VALUES ('krnovo','2026-11-15T13:00:00Z','2026-11-15T14:00:00Z',7.5,7.5,1)")
    conn.execute("INSERT INTO backfill_jobs (created_at_utc, params_json, planned_runs_json, status) "
                 "VALUES ('2026-11-15T15:00:00Z','{}','[]','completed')")
    conn.execute("INSERT INTO backfill_results VALUES (1,0,'ecmwf_ifs','2026-11-15T12:00:00Z','stored',NULL)")
    conn.execute(
        "INSERT INTO calibrations (site_id, model_id, factor, n_train_hours, n_val_hours, trained_at_utc, status, note) "
        "VALUES ('krnovo','ecmwf_ifs',0.95,250,90,'2026-11-16T00:00:00Z','calibrated','policy note')"
    )
    conn.commit()
    conn.close()


def test_v3_to_v4_migration_preserves_everything(tmp_path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)

    ensure_db(db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "4"

    # v4 tables exist and are empty (no fabricated data)
    assert conn.execute("SELECT COUNT(*) c FROM turbines").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM operational_constraints").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM scenario_revisions").fetchone()["c"] == 0

    # new asset column defaulted safely
    assert conn.execute("SELECT allow_scaled_reference FROM assets WHERE id='krnovo'").fetchone()["allow_scaled_reference"] == 0

    # all v3 data preserved bit-for-bit
    run = conn.execute("SELECT raw_json, run_issued_utc, status FROM forecast_runs WHERE id=1").fetchone()
    assert run["raw_json"] == '{"payload":true}'
    assert run["run_issued_utc"] == "2026-11-15T12:00:00Z"
    assert run["status"] == "ok"
    assert conn.execute("SELECT modeled_power_mw, interval_energy_mwh, status FROM forecast_points").fetchone()["modeled_power_mw"] == 12.5
    assert conn.execute("SELECT energy_mwh FROM actual_points").fetchone()["energy_mwh"] == 7.5
    assert conn.execute("SELECT COUNT(*) c FROM actual_imports").fetchone()["c"] == 1
    job = conn.execute("SELECT status, site_id FROM backfill_jobs WHERE id=1").fetchone()
    assert job["status"] == "completed" and job["site_id"] == "krnovo"
    assert conn.execute("SELECT COUNT(*) c FROM backfill_results").fetchone()["c"] == 1
    cal = conn.execute("SELECT factor, status, note FROM calibrations").fetchone()
    assert cal["factor"] == 0.95 and cal["status"] == "calibrated" and cal["note"] == "policy note"
    conn.close()


def test_v4_migration_is_repeatable(tmp_path):
    db = tmp_path / "v3b.db"
    _make_v3_db(db)
    ensure_db(db)
    ensure_db(db)  # restart / repeat
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert conn.execute("SELECT COUNT(*) c FROM turbines").fetchone()[0] == 0
    conn.close()
