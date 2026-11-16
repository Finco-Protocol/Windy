"""Multi-asset tests: creation validation, curve policy, strict site isolation."""

import io

from tests.conftest import make_raw_run

TIMES = [f"2026-11-14T{h:02d}:00" for h in range(6)]
WS_GFS = {"80": [6.0] * 6, "100": [6.4] * 6}


SITE_B = {
    "id": "site-b",
    "label": "Test Site B",
    "latitude": 45.0,
    "longitude": 16.0,
    "capacity_mw": 20.0,
    "turbine_count": 10,
    "turbine_rated_mw": 2.0,
    "hub_height_m": 80.0,
    "loss_factor": 0.05,
    "timezone": "Europe/Zagreb",
    "power_curve_id": "ge103-2750-oedb",
}


def test_create_asset_validation(client):
    api, _fake, _s = client
    curves = api.get("/api/power-curves").json()["curves"]
    assert any(c["id"] == "ge103-2750-oedb" and c["site_specific"] is False for c in curves)

    ok = api.post("/api/assets", json=SITE_B)
    assert ok.status_code == 200, ok.text
    site = ok.json()
    assert site["id"] == "site-b" and site["power_curve_id"] == "ge103-2750-oedb"

    listed = {a["id"]: a for a in api.get("/api/assets").json()["assets"]}
    assert listed["site-b"]["curve_reference_only"] is True  # generic curve → reference label
    assert listed["site-b"]["curve_label"]

    # invalid variants
    assert api.post("/api/assets", json={**SITE_B, "id": None, "label": "X!"}).status_code == 400  # bad slug from label
    assert api.post("/api/assets", json={**SITE_B, "latitude": 999}).status_code == 400
    assert api.post("/api/assets", json={**SITE_B, "hub_height_m": -5}).status_code == 400
    assert api.post("/api/assets", json={**SITE_B, "timezone": "Mars/Olympus"}).status_code == 400
    assert api.post("/api/assets", json={**SITE_B, "power_curve_id": "missing-curve"}).status_code == 400
    assert api.post("/api/assets", json={**SITE_B}).status_code == 400  # duplicate id
    assert api.post("/api/assets", json={**SITE_B, "id": "site-c", "loss_factor": 1.5}).status_code == 400


def test_default_asset_untouched_after_creation(client):
    api, _fake, _s = client
    before = api.get("/api/site").json()["site"]
    api.post("/api/assets", json=SITE_B)
    after = api.get("/api/site").json()["site"]
    assert before == after, "creating a new asset must never touch the default asset"


def _seed_site_b_forecast(api, fake):
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(TIMES, WS_GFS))
    r = api.post(
        "/api/backfill",
        json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0, "days": 2, "site_id": "site-b"},
    )
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_site_isolation_forecasts_actuals_exports(client):
    api, fake, settings = client
    api.post("/api/assets", json=SITE_B)
    run_b = _seed_site_b_forecast(api, fake)

    # site A (default) sees none of site B's runs; its run list is empty
    assert api.get("/api/runs", params={"site_id": "krnovo"}).json()["runs"] == []
    runs_b = api.get("/api/runs", params={"site_id": "site-b"}).json()["runs"]
    assert len(runs_b) == 1 and runs_b[0]["id"] == run_b

    # cross-site run detail is a 404 for the other site's caller
    assert api.get(f"/api/runs/{run_b}", params={"site_id": "krnovo"}).status_code == 404
    assert api.get(f"/api/runs/{run_b}", params={"site_id": "site-b"}).status_code == 200
    r = api.get("/api/compare", params={"run_id": run_b, "target_date": "2026-11-14", "site_id": "krnovo"})
    assert r.status_code == 404

    # actuals import to site B leaves site A empty (hour 14T01: lead 1 h after
    # the run's issue hour 14T00, so it actually pairs)
    csv_bytes = b"ts,mwh\n2026-11-14T01:00:00Z,5\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    r = api.post(
        "/api/actuals/import",
        files={"file": ("b.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace", "site_id": "site-b"},
    )
    assert r.status_code == 200 and r.json()["imported"] is True
    assert api.get("/api/actuals", params={"site_id": "krnovo"}).json()["rows"] == []
    assert len(api.get("/api/actuals", params={"site_id": "site-b"}).json()["rows"]) == 1

    # accuracy is per-site: B has pairs, A has none
    acc_b = api.get("/api/accuracy", params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z", "site_id": "site-b"}).json()
    acc_a = api.get("/api/accuracy", params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"}).json()
    assert acc_a["models"] == {}
    assert "gfs_seamless" in acc_b["models"]

    # calibration trained on B never applies to A
    api.post("/api/calibration/train", json={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z", "site_id": "site-b"})
    cal_a = api.get("/api/calibration").json()["models"]
    cal_b = api.get("/api/calibration", params={"site_id": "site-b"}).json()["models"]
    assert cal_a == [] and len(cal_b) == 1

    # exports are site-scoped
    exp_a = api.get("/api/export/forecast.csv")
    exp_b = api.get("/api/export/forecast.csv", params={"site_id": "site-b"})
    assert len(exp_a.text.strip().splitlines()) == 1  # header only
    assert len(exp_b.text.strip().splitlines()) == 7  # header + 6 points

    # archive health is per-site
    health_b = {m["model_id"]: m for m in api.get("/api/archive/health", params={"site_id": "site-b"}).json()["models"]}
    health_a = {m["model_id"]: m for m in api.get("/api/archive/health").json()["models"]}
    assert health_b["gfs_seamless"]["known_issue_cycles"] == 1
    assert health_a["gfs_seamless"]["known_issue_cycles"] == 0


def test_collect_unknown_site_404(client):
    api, _fake, _s = client
    assert api.post("/api/collect", json={"site_id": "ghost"}).status_code == 404
    assert api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0, "site_id": "ghost"}).status_code == 404


def test_batch_job_is_site_scoped(client):
    api, fake, _s = client
    api.post("/api/assets", json=SITE_B)
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(TIMES, WS_GFS))
    r = api.post(
        "/api/backfill/batch",
        json={
            "models": ["gfs_seamless"],
            "date_from": "2026-11-14",
            "date_to": "2026-11-14",
            "cycles": [0],
            "days": 2,
            "max_requests": 5,
            "site_id": "site-b",
        },
    )
    job_id = r.json()["job_id"]
    from tests.conftest import wait_for_job_status

    job = wait_for_job_status(api, job_id, {"completed"})
    assert job["n_stored"] == 1
    assert job["site_id"] == "site-b"
    assert api.get("/api/backfill/batch", params={"site_id": "krnovo"}).json()["jobs"] == []
    assert len(api.get("/api/backfill/batch", params={"site_id": "site-b"}).json()["jobs"]) == 1
