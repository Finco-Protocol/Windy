"""Batch historical backfill tests: bounds, dry-run, execution statuses,
immutability, consecutive-failure pause, cancel/resume, restart safety."""

import sqlite3
import threading

from app.backfill_batch import mark_interrupted_jobs_paused, parse_params, plan_runs
from tests.conftest import make_raw_run, wait_for_job_status

TIMES = [f"2026-11-10T{h:02d}:00" for h in range(6)]
WS = {"80": [6.0] * 6, "100": [6.4] * 6}


def _body(**over):
    body = {
        "models": ["gfs_seamless"],
        "date_from": "2026-11-10",
        "date_to": "2026-11-10",
        "cycles": [0, 12],
        "days": 2,
        "max_requests": 10,
    }
    body.update(over)
    return body


# ---- parameter validation and planning ---------------------------------------


def test_batch_param_validation(client):
    api, _fake, _s = client
    assert api.post("/api/backfill/batch", json=_body(date_from="2026-11-10", date_to="2026-12-30")).status_code == 400  # range > 31 d
    assert api.post("/api/backfill/batch", json=_body(date_from="2030-01-01", date_to="2030-01-02")).status_code == 400  # future
    assert api.post("/api/backfill/batch", json=_body(cycles=[5])).status_code == 400
    assert api.post("/api/backfill/batch", json=_body(max_requests=999)).status_code == 400
    assert api.post("/api/backfill/batch", json=_body(models=["nope"])).status_code == 404
    assert api.post("/api/backfill/batch", json=_body(date_from="2026-11-10", date_to="2026-11-09")).status_code == 400


def test_plan_respects_request_cap():
    params = parse_params(
        {
            "models": ["gfs_seamless", "ecmwf_ifs"],
            "date_from": "2026-11-01",
            "date_to": "2026-11-05",  # 5 days × 4 cycles × 2 models = 40 runs
            "cycles": [0, 6, 12, 18],
            "days": 10,
            "max_requests": 7,
        }
    )
    planned = plan_runs(params)
    assert len(planned) == 40
    n_active = sum(1 for p in planned if not p["skipped"])
    n_skipped = sum(1 for p in planned if p["skipped"])
    assert (n_active, n_skipped) == (7, 33)
    # request_days clamped per model: icon_eu caps at 5 even if days=10
    params_eu = parse_params({"models": ["icon_eu"], "date_from": "2026-11-01", "date_to": "2026-11-01", "cycles": [0], "days": 10, "max_requests": 5})
    assert plan_runs(params_eu)[0]["request_days"] == 5


def test_dry_run_makes_no_requests_and_reports_plan(client):
    api, fake, _s = client
    resp = api.post("/api/backfill/batch", json=_body(dry_run=True))
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["total_planned"] == 2 and body["total_skipped"] == 0
    assert body["per_model"][0]["model_id"] == "gfs_seamless"
    assert fake.requests == [], "dry run must not hit the provider"
    assert api.get("/api/backfill/batch").json()["jobs"] == [], "dry run must not create a job"


# ---- execution -----------------------------------------------------------------


def test_batch_execution_statuses(client):
    api, fake, _s = client
    fake.push_run("gfs_seamless", "2026-11-10T00:00", make_raw_run(TIMES, WS))  # stored
    fake.push_run("gfs_seamless", "2026-11-10T12:00", make_raw_run(TIMES, WS))  # stored; re-run below → already-archived

    r = api.post("/api/backfill/batch", json=_body())
    job_id = r.json()["job_id"]
    job = wait_for_job_status(api, job_id, {"completed"})
    assert job["status"] == "completed"
    assert job["n_stored"] == 2

    # re-run the same window: everything already archived, values unchanged
    r2 = api.post("/api/backfill/batch", json=_body())
    job2 = wait_for_job_status(api, r2.json()["job_id"], {"completed"})
    assert job2["n_already"] == 2 and job2["n_stored"] == 0
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs) == 2, "batch must not duplicate archived issues"


def test_batch_mixed_statuses_and_immutability(client):
    import json as _json

    api, fake, settings = client
    fake.push_run("gfs_seamless", "2026-11-10T00:00", make_raw_run(TIMES, WS))
    # 12:00 missing from the fake archive → unavailable
    body = _body()
    r = api.post("/api/backfill/batch", json=body)
    job = wait_for_job_status(api, r.json()["job_id"], {"completed"})
    statuses = {res["run_issued_utc"]: res["status"] for res in job["results"]}
    assert statuses["2026-11-10T00:00"] == "stored"
    assert statuses["2026-11-10T12:00"] == "unavailable"
    assert job["n_stored"] == 1 and job["n_unavailable"] == 1

    # provider now "serves" different values for the same issue → archive immutable
    fake.push_run("gfs_seamless", "2026-11-10T00:00", make_raw_run(TIMES, {"80": [20.0] * 6, "100": [21.0] * 6}))
    r2 = api.post("/api/backfill/batch", json=body)
    wait_for_job_status(api, r2.json()["job_id"], {"completed"})
    conn = sqlite3.connect(settings.db_path)
    vals = conn.execute(
        "SELECT wind_speed_mps FROM forecast_points fp JOIN forecast_runs fr ON fr.id=fp.run_id "
        "WHERE fr.model_id='gfs_seamless' AND fr.run_issued_utc='2026-11-10T00:00:00Z' ORDER BY valid_at_utc"
    ).fetchall()
    raw = conn.execute(
        "SELECT raw_json FROM forecast_runs WHERE model_id='gfs_seamless' AND run_issued_utc='2026-11-10T00:00:00Z'"
    ).fetchone()[0]
    conn.close()
    assert all(6.0 <= v[0] <= 6.4 for v in vals), "archived points must not be rewritten by a batch"
    assert "20.0" not in raw, "raw payload must remain the original"


def test_batch_consecutive_failures_pause(client):
    api, fake, _s = client
    for d in ("2026-11-10", "2026-11-11"):
        for cyc in (0, 6, 12, 18):
            fake.om_error_runs.add(("gfs_seamless", f"{d}T{cyc:02d}:00"))
    body = _body(date_from="2026-11-10", date_to="2026-11-11", cycles=[0, 6, 12, 18], max_requests=10)
    r = api.post("/api/backfill/batch", json=body)
    job = wait_for_job_status(api, r.json()["job_id"], {"paused", "failed"}, timeout_s=10)
    assert job["status"] == "paused"
    assert job["n_failed"] == 5  # CONSECUTIVE_FAIL_PAUSE
    assert "consecutive failures" in job["last_error"]


def test_batch_cancel_and_resume_from_cursor(client):
    api, fake, _s = client
    for cyc in (0, 12):
        fake.push_run("gfs_seamless", f"2026-11-10T{cyc:02d}:00", make_raw_run(TIMES, WS))
    fake.push_run("gfs_seamless", "2026-11-11T00:00", make_raw_run(TIMES, WS))
    fake.gates[("gfs_seamless", "2026-11-10T12:00")] = threading.Event()

    body = _body(date_from="2026-11-10", date_to="2026-11-11")
    job_id = api.post("/api/backfill/batch", json=body).json()["job_id"]
    # task 0 runs; task 1 blocks on its gate
    import time

    time.sleep(0.4)
    api.post(f"/api/backfill/batch/{job_id}/cancel")
    fake.gates[("gfs_seamless", "2026-11-10T12:00")].set()
    job = wait_for_job_status(api, job_id, {"cancelled"}, timeout_s=10)
    assert job["status"] == "cancelled"
    assert job["n_stored"] == 2  # in-flight task completed; third task never started
    assert job["cursor_index"] == 2

    # resume from the persisted cursor: only the remaining task runs
    fake.requests.clear()
    api.post(f"/api/backfill/batch/{job_id}/resume")
    job = wait_for_job_status(api, job_id, {"completed"}, timeout_s=10)
    assert job["status"] == "completed" and job["n_stored"] == 3
    assert ("gfs_seamless", "2026-11-10T00:00") not in fake.requests, "resume must not redo checkpointed tasks"


def test_interrupted_jobs_marked_paused(client):
    api, _fake, settings = client
    from app import db as dbm

    with dbm.session(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO backfill_jobs (created_at_utc, params_json, planned_runs_json, status) "
            "VALUES ('2026-01-01T00:00:00Z', '{}', '[]', 'running')"
        )
    n = mark_interrupted_jobs_paused(settings.db_path)
    assert n == 1
    job = api.get("/api/backfill/batch/1").json()
    assert job["status"] == "paused"
    assert "interrupted" in job["last_error"]


def test_resume_rejects_running_job(client):
    api, fake, _s = client
    fake.push_run("gfs_seamless", "2026-11-10T00:00", make_raw_run(TIMES, WS))
    job_id = api.post("/api/backfill/batch", json=_body()).json()["job_id"]
    wait_for_job_status(api, job_id, {"completed"})
    # completed job cannot be resumed either
    assert api.post(f"/api/backfill/batch/{job_id}/resume").status_code == 400
    assert api.get("/api/backfill/batch/999").status_code == 404
