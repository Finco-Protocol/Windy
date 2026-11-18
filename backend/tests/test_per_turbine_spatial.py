"""Per-turbine default backend acceptance, synthetic multi-location NWP data.

No upstream calls. Every 26-coordinate response has distinct wind, and the
single-spot archive remains byte-for-byte unchanged after spatial collection.
"""
from datetime import datetime, timedelta, timezone
import io
import sqlite3

from openpyxl import load_workbook

from tests.conftest import make_raw_run


def prepare(client, *, hours=30, with_inventory=True):
    api, fake, settings = client
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stamps = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    issued = start.strftime("%Y-%m-%dT%H:00")
    fake.push_run("ecmwf_ifs", issued, make_raw_run(stamps, {100: [8.5] * hours}))
    if with_inventory:
        inv = api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.85})
        assert inv.status_code == 200, inv.text
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE assets SET allow_scaled_reference=1 WHERE id='krnovo'")
    return api, settings


def test_without_snapshots_never_falls_back_to_single_spot(client):
    api, settings = prepare(client)
    assert api.post("/api/collect", json={"days": 16}).status_code == 200
    before = api.get("/api/forecast?days=7").json()["models"]
    assert next(m for m in before if m["model_id"] == "ecmwf_ifs")["run"] is not None

    per = api.get("/api/turbine-forecast?days=7")
    assert per.status_code == 200, per.text
    result = per.json()
    assert result["mode"] == "per_turbine"
    assert result["location_info"]["n_turbines"] == 26
    assert result["location_info"]["provisional_mapping"]
    m = next(m for m in result["models"] if m["model_id"] == "ecmwf_ifs")
    assert m["status"] == "NO_SPATIAL_SNAPSHOT"
    assert m["daily"] == [] and m["turbines"] == []


def test_collect_26_coordinate_snapshot_without_changing_spot_points(client):
    api, settings = prepare(client)
    assert api.post("/api/collect", json={"days": 16}).status_code == 200
    with sqlite3.connect(settings.db_path) as conn:
        original = conn.execute(
            "SELECT run_id, valid_at_utc, wind_speed_mps, modeled_power_mw "
            "FROM forecast_points ORDER BY run_id,valid_at_utc"
        ).fetchall()

    r = api.post("/api/collect/turbines", json={"days": 16})
    assert r.status_code == 200, r.text
    reports = r.json()["per_turbine"]
    m = next(x for x in reports["models"] if x["model_id"] == "ecmwf_ifs")
    assert m["status"] == "OK" and m["unique_grid_cells"] == 26
    with sqlite3.connect(settings.db_path) as conn:
        after = conn.execute(
            "SELECT run_id, valid_at_utc, wind_speed_mps, modeled_power_mw "
            "FROM forecast_points ORDER BY run_id,valid_at_utc"
        ).fetchall()
        assert after == original
        assert conn.execute("SELECT COUNT(*) FROM turbine_forecast_snapshots").fetchone()[0] == 1
    again = api.post("/api/collect/turbines", json={"days": 16}).json()
    new = next(x for x in again["per_turbine"]["models"] if x["model_id"] == "ecmwf_ifs")
    assert new["status"] == "ALREADY_ARCHIVED"
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turbine_forecast_snapshots").fetchone()[0] == 1


def test_per_turbine_wind_varies_and_correctly_sums_26_production(client):
    api, _settings = prepare(client)
    api.post("/api/collect/turbines", json={"days": 16})
    r = api.get("/api/turbine-forecast?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    model = next(m for m in body["models"] if m["model_id"] == "ecmwf_ifs")
    assert model["status"] == "OK"
    assert len(model["turbines"]) == 26
    a, b = model["turbines"][0], model["turbines"][-1]
    assert a["unit_id"] == "WTG-001" and b["unit_id"] == "WTG-026"
    assert a["latitude"] != b["latitude"]
    assert a["daily"][0]["wind_mean_mps"] != b["daily"][0]["wind_mean_mps"]
    totals = [m for m in model["daily"] if m["hours_counted"] > 0]
    assert totals
    one_day = totals[0]
    sum_units = sum((u["daily"][0]["base_mwh"] or 0) for u in model["turbines"])
    assert abs(one_day["base_mwh"] - sum_units) < 0.1
    assert one_day["adjusted_mwh"] == one_day["base_mwh"]


def test_offline_turbine_changes_adjusted_only_not_spatial_baseline(client):
    api, _settings = prepare(client)
    api.post("/api/collect/turbines", json={"days": 16})
    before = api.get("/api/turbine-forecast?days=7").json()
    baseline = next(m for m in before["models"] if m["model_id"] == "ecmwf_ifs")["daily"][0]["base_mwh"]
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = api.post("/api/assets/krnovo/constraints", json={"rows": [
        {"kind": "offline", "unit_ids": ["WTG-001"], "start_local": start}
    ]})
    assert r.status_code == 200, r.text
    after = api.get("/api/turbine-forecast?days=7").json()
    model = next(m for m in after["models"] if m["model_id"] == "ecmwf_ifs")
    assert model["daily"][0]["base_mwh"] == baseline
    assert model["daily"][0]["adjusted_mwh"] < baseline


def test_spatial_export_is_real_xlsx_not_one_spot_and_validates_options(client):
    api, _settings = prepare(client)
    api.post("/api/collect/turbines", json={"days": 16})
    r = api.get("/api/assets/krnovo/turbine-forecast-export.xlsx?days=7&unit=mwh&mode=adjusted")
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"PK")
    book = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    assert "Farm MWH" in book.sheetnames
    assert "WTG MWH" in book.sheetnames
    assert "Pokrivenost" in book.sheetnames
    assert "Metapodaci" in book.sheetnames
    rows = list(book["WTG MWH"].iter_rows(values_only=True))
    assert any(row[2] == "WTG-026" for row in rows[1:])
    bad = api.get("/api/assets/krnovo/turbine-forecast-export.xlsx?days=7&unit=invalid")
    assert bad.status_code == 400



def test_default_26_mode_hourly_charts_use_spatial_not_one_spot(client):
    api, _settings = prepare(client)
    r = api.post("/api/collect/turbines", json={"days": 16})
    assert r.status_code == 200, r.text
    spot = api.get("/api/forecast?days=7&modeling_mode=spot").json()
    spatial = api.get("/api/forecast?days=7&modeling_mode=per_turbine&with_availability=true")
    assert spatial.status_code == 200, spatial.text
    by_id = {x["model_id"]: x for x in spatial.json()["models"]}
    ecm = by_id["ecmwf_ifs"]
    spot_ecm = next(m for m in spot["models"] if m["model_id"] == "ecmwf_ifs")
    assert ecm["spatial"]["status"] == "OK"
    assert ecm["spatial"]["grid_cells"] == 26
    assert ecm["run"]["id"] == spot_ecm["run"]["id"]
    assert ecm["points"] and spot_ecm["points"]
    assert ecm["points"][0]["modeled_power_mw"] != spot_ecm["points"][0]["modeled_power_mw"]
    assert ecm["points"][0]["wind_speed_mps"] != spot_ecm["points"][0]["wind_speed_mps"]
    assert ecm["adjusted"]["available"]
    assert ecm["adjusted"]["points"][0]["adjusted_mw"] == ecm["points"][0]["modeled_power_mw"]
    assert ecm["daily_local"], "existing daily chart must use spatially aggregated hours"
    assert spatial.json()["intersection"]["n_common_hours"] > 0


def test_spatial_forecast_missing_snapshot_discloses_gap_no_spot_fallback(client):
    api, _settings = prepare(client)
    api.post("/api/collect", json={"days": 16})
    m = next(m for m in api.get(
        "/api/forecast?days=7&modeling_mode=per_turbine&with_availability=true"
    ).json()["models"] if m["model_id"] == "ecmwf_ifs")
    assert m["run"] is not None, "issue time survives for explicit gap disclosure"
    assert m["points"] == [] and m["daily_local"] == []
    assert m["adjusted"]["available"] is False
    assert any("1-spot" in w for w in m["warnings"])
    spot = next(m for m in api.get("/api/forecast?days=7&modeling_mode=spot").json()["models"]
                if m["model_id"] == "ecmwf_ifs")
    assert spot["points"], "original spot option stays intact"


def test_invalid_spatial_mode_is_400_without_mutation(client):
    api, settings = prepare(client)
    assert api.get("/api/forecast?days=7&modeling_mode=invalid").status_code == 400
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turbine_forecast_snapshots").fetchone()[0] == 0
