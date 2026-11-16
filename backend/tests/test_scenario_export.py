"""Scenario CSV export test: pinned revision hourly values with provenance."""

import pytest

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
    # V1.4 correction: hourly MWh columns + provenance/coverage flags
    for col in ("baseline_mwh", "scenario_base_mwh", "adjusted_mwh", "curve_mode", "baseline_comparable"):
        assert col in a_lines[0], col
    assert len(a_lines) == 7  # header + 6 hourly rows
    assert ",45.0," in a or ",45," in a  # adjusted_mw column reflects A's cap
    assert "60.0" not in a.splitlines()[1]
    assert ",60.0," in b or ",60," in b
    # hourly MWh = MW × 1 h for this fixture
    header = a_lines[0].split(",")
    row = a_lines[1].split(",")
    mw_i = header.index("baseline_mw")
    mwh_i = header.index("baseline_mwh")
    assert float(row[mwh_i]) == pytest.approx(float(row[mw_i]), abs=1e-4)

    # cross-site export blocked
    api.post("/api/assets", json={
        "id": "other", "label": "Other", "latitude": 44.0, "longitude": 17.0, "capacity_mw": 71.5,
        "turbine_count": 26, "turbine_rated_mw": 2.75, "hub_height_m": 90,
        "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    })
    r_other = api.get("/api/assets/other/scenario/export.csv", params={"run_id": 1})
    assert r_other.status_code == 404


def test_scenario_export_30min_intervals(client):
    """30-min archived intervals: MWh columns must use the exact stored
    interval duration (MW × 0.5), not an assumed hour."""
    api, fake, _s = client
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(DAY + "T00:00:00+00:00")
    times = [(start + timedelta(minutes=i * 30)).strftime("%Y-%m-%dT%H:%M") for i in range(12)]  # 6 h
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times, {"80": [15.0] * 12, "100": [15.0] * 12}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 2})
    assert r.status_code == 200, r.text

    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    export = api.get("/api/assets/krnovo/scenario/export.csv", params={"run_id": 1})
    lines = export.text.strip().splitlines()
    header = lines[0].split(",")
    ih_i = header.index("interval_hours")
    mwh_i = header.index("adjusted_mwh")
    mw_i = header.index("adjusted_mw")
    # row index 2 (second data row) carries the derived 0.5 h duration; the
    # first row defaults to 1 h by definition. The 45 MW export cap from this
    # test caps the adjusted value (at 15 m/s the pre-export is 71.5 MW).
    second_data = lines[2].split(",")
    assert float(second_data[ih_i]) == 0.5
    assert float(second_data[mw_i]) == pytest.approx(45.0, abs=1e-3)
    assert float(second_data[mwh_i]) == pytest.approx(45.0 * 0.5, abs=1e-3), "MWh must use the exact 0.5 h duration"


def test_scenario_export_2h_intervals(client):
    """>1 h intervals: MWh columns use the exact stored duration (MW × 2)."""
    api, fake, _s = client
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(DAY + "T00:00:00+00:00")
    times = [(start + timedelta(hours=i * 2)).strftime("%Y-%m-%dT%H:00") for i in range(6)]  # 12 h span
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times, {"80": [8.0] * 6, "100": [8.0] * 6}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text

    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    export = api.get("/api/assets/krnovo/scenario/export.csv", params={"run_id": 1})
    lines = export.text.strip().splitlines()
    header = lines[0].split(",")
    ih_i = header.index("interval_hours")
    mwh_i = header.index("baseline_mwh")
    mw_i = header.index("baseline_mw")
    second_data = lines[2].split(",")
    assert float(second_data[ih_i]) == 2.0
    assert float(second_data[mwh_i]) == pytest.approx(float(second_data[mw_i]) * 2.0, abs=1e-4)
