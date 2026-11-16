"""P0-1 regression: scenario revision list/retrieve, pinned reopening,
cross-site isolation, idempotent publish, changed schedules and restart.

These tests execute the SQL (not source-string assertions) — the revisions
endpoint previously crashed with `ambiguous column name: id` on ANY data."""

import sqlite3

from tests.conftest import make_raw_run, wait_for_job_status  # noqa: F401

DAY = "2026-11-14"
HOURS = [f"{DAY}T{h:02d}:00" for h in range(6)]
WS = {"80": [15.0] * 6, "100": [15.0] * 6}

SITE_A = {
    "id": "site-a", "label": "Site A", "latitude": 45.0, "longitude": 16.0,
    "capacity_mw": 71.5, "turbine_count": 26, "turbine_rated_mw": 2.75,
    "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
}
SITE_B = {
    "id": "site-b", "label": "Site B", "latitude": 44.0, "longitude": 17.0,
    "capacity_mw": 71.5, "turbine_count": 26, "turbine_rated_mw": 2.75,
    "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
}


def _setup(api, fake):
    api.post("/api/assets", json=SITE_A)
    api.post("/api/assets", json=SITE_B)
    fake.push_run("ecmwf_ifs", f"{DAY}T00:00", make_raw_run(HOURS, {"100": [15.0] * 6}))
    for site in ("site-a", "site-b"):
        api.post(f"/api/assets/{site}/turbines/generate", json={"count": 26, "rating_mw": 2.75})
        r = api.post(
            "/api/backfill",
            json={"model_id": "ecmwf_ifs", "run_date": DAY, "run_hour": 0, "days": 2, "site_id": site},
        )
        assert r.status_code == 200, r.text
    return api.get("/api/runs", params={"site_id": "site-a", "model_id": "ecmwf_ifs"}).json()["runs"][0]["id"]


def test_revisions_list_and_pinned_reopen(client):
    api, fake, settings = client
    run_id = _setup(api, fake)

    # publish A with a 50 MW export cap
    api.post(f"/api/assets/site-a/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 50, "start_local": "2026-11-13T00:00", "end_local": None}
    ]})
    pub_a = api.post("/api/assets/site-a/scenario/publish", json={"run_id": run_id}).json()

    # change the schedule (delete + add 60) and publish B
    cons = api.get("/api/assets/site-a/constraints").json()
    api.post("/api/assets/site-a/constraints/delete", json={"ids": [c["id"] for c in cons["constraints"]]})
    api.post("/api/assets/site-a/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 60, "start_local": "2026-11-13T00:00", "end_local": None}
    ]})
    pub_b = api.post("/api/assets/site-a/scenario/publish", json={"run_id": run_id}).json()
    assert pub_a["scenario_revision_id"] != pub_b["scenario_revision_id"]

    # LIST executes the JOIN and returns both revisions with pinned retrieval
    lst = api.get("/api/assets/site-a/scenario/revisions").json()["revisions"]
    assert [r["id"] for r in lst] == [pub_b["scenario_revision_id"], pub_a["scenario_revision_id"]]
    assert all(r["run_issued_utc"] == f"{DAY}T00:00:00Z" for r in lst)
    assert all("inventory_revision" in r and "constraints_revision" in r for r in lst)
    assert all(r["retrieve_at"] for r in lst)

    # REOPEN A through its pinned link: reproduces A (50 MW), not the current 60
    a_link = next(r for r in lst if r["id"] == pub_a["scenario_revision_id"])["retrieve_at"]
    reopened = api.get(a_link).json()
    assert reopened["inventory_revision"] == pub_a["retrieval"]["inventory_revision"]
    assert reopened["constraints_revision"] == pub_a["retrieval"]["constraints_revision"]
    assert reopened["points"][0]["adjusted_mw"] == 50.0
    assert reopened["summary"]["windows"]["24h"]["available_hours_subtotal"]["adjusted_mwh"] == 6 * 50.0

    # reopen B: 60 MW
    b_link = next(r for r in lst if r["id"] == pub_b["scenario_revision_id"])["retrieve_at"]
    reopened_b = api.get(b_link).json()
    assert reopened_b["points"][0]["adjusted_mw"] == 60.0


def test_revisions_cross_site_isolation(client):
    api, fake, _s = client
    run_id = _setup(api, fake)
    api.post("/api/assets/site-a/scenario/publish", json={"run_id": run_id})

    # site-b sees no revisions (its own run has none published)
    assert api.get("/api/assets/site-b/scenario/revisions").json()["revisions"] == []
    # the pinned retrieval is site-scoped: site-b cannot read site-a's run
    r = api.get(f"/api/assets/site-b/scenario", params={"run_id": run_id})
    assert r.status_code == 404


def test_publish_idempotent_and_restart_safe(client):
    api, fake, settings = client
    run_id = _setup(api, fake)
    p1 = api.post("/api/assets/site-a/scenario/publish", json={"run_id": run_id}).json()
    p2 = api.post("/api/assets/site-a/scenario/publish", json={"run_id": run_id}).json()
    assert p1["scenario_revision_id"] == p2["scenario_revision_id"]
    assert p1["created_at_utc"] == p2["created_at_utc"], "republish must not rewrite the published row"

    # "restart": a fresh app instance over the same DB file must still list and
    # reopen the published revisions (append-only rows survive the process)
    from app.main import create_app

    application = create_app(settings=settings, om_client=fake)
    from fastapi.testclient import TestClient

    with TestClient(application) as api2:
        lst = api2.get("/api/assets/site-a/scenario/revisions").json()["revisions"]
        assert len(lst) == 1
        reopened = api2.get(lst[0]["retrieve_at"]).json()
        assert reopened["points"][0]["adjusted_mw"] == 71.5  # no constraints → full
