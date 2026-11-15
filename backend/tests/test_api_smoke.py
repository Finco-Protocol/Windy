"""API smoke tests: health, site provenance, export CSV, run detail."""

import io

from tests.conftest import make_raw_run


def test_health(client):
    api, _fake, settings = client
    h = api.get("/api/health").json()
    assert h["ok"] is True
    assert h["schema_version"] == "1"
    assert str(settings.db_path) == h["db_path"]


def test_site_shows_assumption_provenance(client):
    api, _fake, _settings = client
    site = api.get("/api/site").json()
    assert site["site"]["id"] == "krnovo"
    assert site["site"]["capacity_mw"] == 71.5
    assert site["site"]["hub_height_m"] == 90.0
    assumptions = site["site"]["assumptions"]
    assert "PROVISIONAL" in assumptions["hub_height_m"]
    assert "71.5 MW = 26 × 2.75 MW" in assumptions["capacity_mw"]
    assert site["power_curve"]["status"] in ("manufacturer-reference", "demo-illustrative")
    ids = [m["id"] for m in site["models"]]
    assert ids == ["ecmwf_ifs", "gfs_seamless", "icon_global", "icon_eu"]
    assert "open-meteo" in site["source_note"].lower()


def test_forecast_empty_state_is_explicit(client):
    api, _fake, _settings = client
    fc = api.get("/api/forecast", params={"days": 7}).json()
    assert fc["models"][0]["run"] is None
    assert any("Fetch now" in w for w in fc["models"][0]["warnings"])
    assert fc["intersection"]["n_common_hours"] == 0


def test_run_detail_and_export(client):
    api, fake, _settings = client
    times = [f"2026-11-14T{h:02d}:00" for h in range(3)]
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(times, {"80": [6.0] * 3, "100": [6.4] * 3}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0})
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]
    run_id = runs[0]["id"]

    detail = api.get(f"/api/runs/{run_id}").json()
    assert detail["model_id"] == "gfs_seamless"
    assert len(detail["points"]) == 3
    assert "raw_json" not in detail, "raw payload must not be dumped into the API"

    export = api.get("/api/export/forecast.csv", params={"run_id": run_id})
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    header = export.text.splitlines()[0]
    for col in ("model_id", "run_issued_utc", "valid_at_utc", "wind_speed_mps", "wind_direction_deg", "modeled_power_mw", "interval_energy_mwh", "status"):
        assert col in header
    assert len(export.text.splitlines()) == 4  # header + 3 points


def test_actuals_export(client):
    api, _fake, _settings = client
    csv_bytes = b"ts,mwh\n2026-11-14T00:00:00Z,1.5\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "reject"},
    )
    export = api.get("/api/export/actuals.csv")
    assert export.status_code == 200
    lines = export.text.strip().splitlines()
    assert lines[0] == "interval_start_utc,interval_end_utc,energy_mwh,power_mw,import_id"
    assert len(lines) == 2 and "1.5" in lines[1]
