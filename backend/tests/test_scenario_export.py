"""Scenario CSV export test: pinned revision hourly values with provenance."""

from tests.conftest import make_raw_run

DAY = "2026-11-13"
TIMES = [f"{DAY}T{h:02d}:00" for h in range(6)]


def test_scenario_export_csv_pinned_revision(client):
    api, fake, _s = client
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(TIMES, {"80": [15.0] * 6, "100": [15.0] * 6}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 2})
    assert r.status_code == 200, r.text

    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    pub_a = api.post("/api/assets/krnovo/scenario/publish", json={"run_id": 1}).json()

    # change the cap to 60 and publish B
    cons = api.get("/api/assets/krnovo/constraints").json()["constraints"]
    api.post("/api/assets/krnovo/constraints/delete", json={"ids": [c["id"] for c in cons]})
    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 60, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    pub_b = api.post("/api/assets/krnovo/scenario/publish", json={"run_id": 1}).json()

    # export A pinned: 45 MW rows; export B pinned: 60 MW rows
    a = api.get(pub_a["points_available_at"].replace("/scenario?", "/scenario/export.csv?")).text
    b = api.get(pub_b["points_available_at"].replace("/scenario?", "/scenario/export.csv?")).text

    a_lines = a.strip().splitlines()
    assert a_lines[0].startswith("site_id,model_id,run_issued_utc,inventory_revision,constraints_revision")
    assert "algorithm_version" in a_lines[0] and "availability_loss_mwh" in a_lines[0] and "status" in a_lines[0]
    assert len(a_lines) == 7  # header + 6 hourly rows
    assert ",45.0," in a or ",45," in a  # adjusted_mw column reflects A's cap
    assert "60.0" not in a.splitlines()[1]
    assert ",60.0," in b or ",60," in b

    # cross-site export blocked
    api.post("/api/assets", json={
        "id": "other", "label": "Other", "latitude": 44.0, "longitude": 17.0, "capacity_mw": 71.5,
        "turbine_count": 26, "turbine_rated_mw": 2.75, "hub_height_m": 90,
        "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    })
    r_other = api.get("/api/assets/other/scenario/export.csv", params={"run_id": 1})
    assert r_other.status_code == 404
