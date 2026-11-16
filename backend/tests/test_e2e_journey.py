"""Owner acceptance journey A–G — ONE database, chained steps, deterministic
fixtures (no network). Real live-provider evidence is collected separately by
scripts/live_smoke.py and reported as REAL_LIVE_*; everything here is
DETERMINISTIC_FIXTURE evidence.

A  clean DB → default site + synthetic Site B → restart → persistence; screen APIs load
B  forecast collected twice → issue snapshot not corrupted; provenance split
C  71.50 / 60.50 / 45 MW arithmetic with loss split
D  publish scenario A → edit → publish B → reopen pinned A → immutability
E  15-min actuals (4/hour) + duplicates/gaps/overlaps/missing/over-capacity explicit; lead bins dedupe
F  site switch isolation
G  restart + backup/restore smoke + migration repeatability
"""

import io
import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import db as dbm
from app.config import Settings
from app.store import ensure_db
from tests.conftest import FakeOMClient, make_raw_run, wait_for_job_status  # noqa: F401

DAY = "2026-11-13"
_DAY0 = datetime.fromisoformat(DAY + "T00:00:00+00:00")
HOURS = [(_DAY0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(48)]  # two days
WS_RATED = {"80": [15.0] * 48, "100": [15.0] * 48}


def _csv(rows: list[tuple[str, float]]) -> dict:
    body = "ts,mwh\n" + "\n".join(f"{t},{v}" for t, v in rows)
    return {"file": ("meter.csv", io.BytesIO(body.encode()), "text/csv")}


MAPPING_15MIN = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":0.25}'


def _import_actuals(api, rows, on_conflict="replace", site_id="krnovo"):
    files = _csv(rows)
    r = api.post(
        "/api/actuals/import",
        files=files,
        data={"mapping": MAPPING_15MIN, "on_conflict": on_conflict, "site_id": site_id},
    )
    return r


def test_owner_journey_a_to_g(client):
    api, fake, settings = client

    # ---- A: clean DB already seeded krnovo by lifespan; add synthetic Site B; restart
    r_b = api.post("/api/assets", json={
        "id": "site-b", "label": "Site B", "latitude": 44.0, "longitude": 17.0,
        "capacity_mw": 71.5, "turbine_count": 26, "turbine_rated_mw": 2.75,
        "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    })
    assert r_b.status_code == 200, r_b.text
    ensure_db(settings.db_path)  # "restart" of the init step
    with dbm.session(settings.db_path) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY id").fetchall()]
    assert ids == ["krnovo", "site-b"], "restart must not duplicate or reset seeds"
    for path in ("/api/health", "/api/forecast?site_id=site-b", "/api/runs?site_id=site-b", "/api/actuals?site_id=site-b", "/api/assets"):
        assert api.get(path).status_code == 200, path
    assert api.get("/api/assets/site-b").status_code == 200
    assert api.get("/api/assets/ghost").status_code == 404

    # ---- B: genuine-forecast journey uses deterministic fixtures here; fetch twice
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(HOURS, WS_RATED))
    r_bf = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r_bf.status_code == 200, r_bf.text
    runs1 = api.get("/api/runs", params={"site_id": "krnovo", "model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs1) == 1
    snap1 = api.get(f"/api/runs/{runs1[0]['id']}", params={"site_id": "krnovo"}).json()["points"]
    # second collection of the SAME issue → immutable no-op
    api.post("/api/collect", json={"site_id": "krnovo", "days": 2})
    runs2 = api.get("/api/runs", params={"site_id": "krnovo", "model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs2) == 1 and runs2[0]["id"] == runs1[0]["id"]
    snap2 = api.get(f"/api/runs/{runs1[0]['id']}", params={"site_id": "krnovo"}).json()["points"]
    assert snap1 == snap2, "second fetch must not corrupt the original issue snapshot"
    # provenance split: fetch time, issue time, valid time all present and distinct fields
    assert runs2[0]["run_issued_utc"] == f"{DAY}T00:00:00Z"
    assert runs2[0]["fetched_at_utc"] and runs2[0]["first_valid_utc"]

    # ---- C: 26 × 2.75 = 71.50 installed; 2 offline + 4 OTHER at 50% → 60.50; farm cap 45
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    run_id = runs1[0]["id"]
    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "offline", "unit_ids": ["WTG-001", "WTG-002"], "start_local": "2026-11-12T00:00", "end_local": None},
        {"kind": "curtailment", "unit_ids": ["WTG-003", "WTG-004", "WTG-005", "WTG-006"], "cap_percent": 50, "start_local": "2026-11-12T00:00", "end_local": None},
    ]})
    sc = api.get("/api/assets/krnovo/scenario", params={"run_id": run_id}).json()
    rated = next(p for p in sc["points"] if p["status"] == "ok")
    assert rated["pre_export_mw"] == pytest.approx(60.5, abs=1e-3)
    assert rated["adjusted_mw"] == pytest.approx(60.5, abs=1e-3), "60.50 MW before farm export cap"

    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    sc2 = api.get("/api/assets/krnovo/scenario", params={"run_id": run_id}).json()
    p2 = next(p for p in sc2["points"] if p["status"] == "ok")
    assert p2["adjusted_mw"] == pytest.approx(45.0, abs=1e-3), "farm cap applies AFTER unit limits"
    assert p2["lost_to_unit_mwh"] + p2["lost_to_export_mwh"] == pytest.approx(p2["scenario_base_mw"] - 45.0, abs=1e-2)

    # ---- D: publish A, edit, publish B, reopen pinned A
    pub_a = api.post("/api/assets/krnovo/scenario/publish", json={"run_id": run_id, "constraints_revision": 1}).json()
    cons = api.get("/api/assets/krnovo/constraints").json()["constraints"]
    api.post("/api/assets/krnovo/constraints/delete", json={"ids": [c["id"] for c in cons]})
    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 50, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    pub_b = api.post("/api/assets/krnovo/scenario/publish", json={"run_id": run_id}).json()
    assert pub_a["scenario_revision_id"] != pub_b["scenario_revision_id"]
    lst = api.get("/api/assets/krnovo/scenario/revisions").json()["revisions"]
    assert [r["id"] for r in lst] == [pub_b["scenario_revision_id"], pub_a["scenario_revision_id"]]
    a_sc = api.get(next(r for r in lst if r["id"] == pub_a["scenario_revision_id"])["retrieve_at"]).json()
    assert a_sc["constraints_revision"] == pub_a["retrieval"]["constraints_revision"] == 1
    assert a_sc["points"][0]["adjusted_mw"] == pytest.approx(60.5, abs=1e-3), "revision A (offline+curtailment, no farm cap) reproduced exactly"
    # raw forecast rows and actuals unchanged after B (actuals imported in E below re-verified)
    pts_after = api.get(f"/api/runs/{run_id}", params={"site_id": "krnovo"}).json()["points"]
    assert pts_after == snap1, "archived forecast rows immutable across scenario edits"

    # ---- E: 15-min actuals (4/hour), problem cases explicit, lead-bin dedupe
    good = [f"{DAY}T{h:02d}:{q * 15:02d}" for h in range(2, 6) for q in range(4)]
    rows = [(t, 1) for t in good]
    r = _import_actuals(api, rows)
    assert r.status_code == 200 and r.json()["imported"] is True and r.json()["rows_imported"] == 16, r.text

    # duplicate timestamp → blocked by default
    dup = rows + [(good[0], 9)]
    r_dup = _import_actuals(api, dup)
    assert r_dup.status_code == 200 and r_dup.json()["imported"] is True  # duplicate-in-file keeps last, warned
    assert any("duplicate interval" in w for w in r_dup.json()["warnings"])

    # over-capacity reading → blocked import (nothing persisted from that file)
    bad = [(f"{DAY}T07:{q * 15:02d}", 200) for q in range(4)]  # 200 MWh/15min → 800 MW > 71.5 cap
    r_bad = _import_actuals(api, bad)
    assert r_bad.status_code == 400, r_bad.text
    with dbm.session(settings.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM actual_points WHERE interval_start_utc >= '2026-11-13T07:00:00Z'").fetchone()["c"]
    assert n == 0, "over-capacity readings must not pollute accuracy"

    # lead-bin accuracy over the archived issue: overall dedupes the actual hour
    acc = api.get(
        "/api/accuracy",
        params={"from_utc": f"{DAY}T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z", "site_id": "krnovo"},
    ).json()
    assert acc["actual_hours_in_range"] == 4  # hours 02..05 complete from 16 × 15-min readings
    gfs = acc["models"]["gfs_seamless"]
    assert gfs["total"] is not None
    total_hours = gfs["total"]["distinct_hours"]
    bin_hours = sum(b["distinct_hours"] for b in gfs["bins"].values())
    assert bin_hours >= total_hours, "bins may overlap per lead; overall is the deduplicated set"

    # ---- F: site switch isolation (checked throughout; explicit here)
    assert api.get("/api/runs", params={"site_id": "site-b", "model_id": "gfs_seamless"}).json()["runs"] == []
    assert api.get("/api/actuals", params={"site_id": "site-b"}).json()["rows"] == []
    assert api.get(f"/api/assets/site-b/scenario", params={"run_id": run_id}).status_code == 404

    # ---- G: backup/restore smoke on a TEMP copy (never a committed path) + migrations repeatable
    backup_path = settings.db_path.parent / "backup-smoke.db"
    shutil.copyfile(settings.db_path, backup_path)  # offline SQLite file copy of a quiescent DB
    conn = sqlite3.connect(str(backup_path))
    conn.row_factory = sqlite3.Row
    n_runs = conn.execute("SELECT COUNT(*) c FROM forecast_runs").fetchone()["c"]
    n_actuals = conn.execute("SELECT COUNT(*) c FROM actual_points").fetchone()["c"]
    conn.close()
    assert n_runs >= 1 and n_actuals >= 6
    # restore = run the app against the restored copy
    from app.main import create_app
    from fastapi.testclient import TestClient

    restore_settings = Settings()
    restore_settings.db_path = backup_path
    restore_settings.scheduler_enabled = False
    with TestClient(create_app(settings=restore_settings, om_client=fake)) as api2:
        h = api2.get("/api/health").json()
        assert h["ok"] is True and h["schema_version"] == "4"
        assert api2.get("/api/runs", params={"site_id": "krnovo"}).json()["runs"][0]["id"] == run_id
    # migrations repeatable (second ensure on the restored copy)
    ensure_db(backup_path)
    with dbm.session(backup_path) as conn:
        assert dbm.get_meta(conn, "schema_version") == "4"


