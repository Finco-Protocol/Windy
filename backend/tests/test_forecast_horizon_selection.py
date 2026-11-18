"""Regression: 15-day ECMWF / long ICON survives newer short issues.

All evidence is synthetic; no paid or live provider calls.
"""
from datetime import datetime, timedelta, timezone

from app.openmeteo import ModelRunUnavailable, OpenMeteoClient
from app.registry import get_model
from tests.conftest import make_raw_run


BASE = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _issue(hour):
    return (BASE + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:00")


def _raw(issue_hour, hours, model="ecmwf_ifs"):
    begin = BASE + timedelta(hours=issue_hour)
    times = [(begin + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(hours)]
    if model == "ecmwf_ifs":
        return make_raw_run(times, {100: [8.0] * hours})
    return make_raw_run(times, {80: [8.0] * hours, 120: [8.4] * hours})


def _backfill(api, fake, model, issue_hour, hours):
    fake.push_run(model, _issue(issue_hour), _raw(issue_hour, hours, model))
    stamp = BASE + timedelta(hours=issue_hour)
    res = api.post("/api/backfill", json={
        "model_id": model, "run_date": stamp.strftime("%Y-%m-%d"),
        "run_hour": stamp.hour, "days": 16,
    })
    assert res.status_code == 200, res.text
    return res.json()


def _entry(api, model, days=15):
    res = api.get("/api/forecast", params={"site_id": "krnovo", "days": days})
    assert res.status_code == 200, res.text
    return next(m for m in res.json()["models"] if m["model_id"] == model)


def test_ecmwf_older_360h_long_wins_over_newer_90h_short(client):
    api, fake, _settings = client
    full = _backfill(api, fake, "ecmwf_ifs", 0, 360)
    short = _backfill(api, fake, "ecmwf_ifs", 6, 90)
    m = _entry(api, "ecmwf_ifs")
    assert m["run"]["id"] == full["run_id"]
    assert m["run"]["id"] != short["run_id"]
    assert m["run_selection"]["cycle_kind"] == "long"
    assert m["run_selection"]["newer_issue_preserved"] is True
    assert m["run_selection"]["available_lead_hours"] >= 300
    assert len(m["points"]) >= 300
    assert any("noviji kratki" in w.lower() for w in m["warnings"])
    runs = api.get("/api/runs", params={"model_id": "ecmwf_ifs"}).json()["runs"]
    assert {r["id"] for r in runs} == {full["run_id"], short["run_id"]}


def test_ecmwf_newer_full_12z_long_preferred_to_previous_long(client):
    api, fake, _ = client
    _backfill(api, fake, "ecmwf_ifs", 0, 360)
    full = _backfill(api, fake, "ecmwf_ifs", 12, 360)
    _backfill(api, fake, "ecmwf_ifs", 18, 90)
    m = _entry(api, "ecmwf_ifs")
    assert m["run"]["id"] == full["run_id"]
    assert m["run_selection"]["cycle_kind"] == "long"


def test_ecmwf_truncated_00z_7day_archive_never_masquerades_as_long(client):
    api, fake, _ = client
    _backfill(api, fake, "ecmwf_ifs", 0, 168)
    short = _backfill(api, fake, "ecmwf_ifs", 6, 90)
    m = _entry(api, "ecmwf_ifs")
    assert m["run"]["id"] == short["run_id"]
    assert m["run_selection"]["cycle_kind"] == "short"
    assert m["run_selection"]["available_lead_hours"] < 100
    assert "Nema potvrđenog" in m["warnings"][0]
    assert len(m["points"]) <= 90


def test_icon_180h_long_selected_and_max_horizon_truthful(client):
    api, fake, _ = client
    full = _backfill(api, fake, "icon_global", 0, 180)
    _backfill(api, fake, "icon_global", 6, 120)
    m = _entry(api, "icon_global", 10)
    assert m["run"]["id"] == full["run_id"]
    assert m["run_selection"]["cycle_kind"] == "long"
    assert m["run_selection"]["full_horizon_hours"] == 180
    assert m["run_selection"]["available_lead_hours"] >= 150


def test_collect_keeps_two_cycles_and_uses_provider_maximum(client):
    api, fake, _ = client
    fake.push_run("ecmwf_ifs", _issue(0), _raw(0, 360))
    fake.push_run("ecmwf_ifs", _issue(6), _raw(6, 90))
    fake.push_run("icon_global", _issue(0), _raw(0, 180, "icon_global"))
    fake.push_run("icon_global", _issue(6), _raw(6, 120, "icon_global"))
    res = api.post("/api/collect", json={"site_id": "krnovo", "days": 16})
    assert res.status_code == 200, res.text
    out = res.json()
    assert {x["model_id"] for x in out["long_cycles"]} == {"ecmwf_ifs", "icon_global"}
    assert all(x["status"] == "ok" for x in out["long_cycles"])
    assert len(api.get("/api/runs", params={"model_id": "ecmwf_ifs"}).json()["runs"]) == 2
    assert len(api.get("/api/runs", params={"model_id": "icon_global"}).json()["runs"]) == 2
    assert _entry(api, "ecmwf_ifs")["run_selection"]["cycle_kind"] == "long"
    assert _entry(api, "icon_global")["run_selection"]["cycle_kind"] == "long"
    assert next(x for x in out["models"] if x["model_id"] == "ecmwf_ifs")["points"] == 90


def test_collect_without_long_does_not_invent_live_long_issue(client):
    api, fake, _ = client
    fake.push_run("ecmwf_ifs", _issue(6), _raw(6, 90))
    res = api.post("/api/collect", json={"site_id": "krnovo", "days": 16})
    assert res.status_code == 200
    extra = next(x for x in res.json()["long_cycles"] if x["model_id"] == "ecmwf_ifs")
    assert extra["status"] == "UNAVAILABLE"
    assert len(api.get("/api/runs", params={"model_id": "ecmwf_ifs"}).json()["runs"]) == 1
    assert _entry(api, "ecmwf_ifs")["run_selection"]["cycle_kind"] == "short"


def test_openmeteo_filter_avoids_18z_even_when_it_is_newer():
    # Uses a deterministic fake response rather than a network call.
    om = OpenMeteoClient()
    calls = []

    def scripted(_lat, _lon, _spec, iso, _days):
        calls.append(iso)
        if iso.endswith("12:00"):
            return {"hourly": {"time": ["2026-11-14T12:00"]}}
        raise ModelRunUnavailable("unavailable")

    om.fetch_single_run = scripted
    try:
        iso, _raw_body = om.latest_available_run(
            42.8756, 19.1106, get_model("ecmwf_ifs"), 15,
            now=datetime(2026, 11, 14, 19, tzinfo=timezone.utc),
            max_probes=8, run_hour_filter=(0, 12),
        )
        assert iso == "2026-11-14T12:00"
        assert all(int(x[11:13]) in (0, 12) for x in calls)
    finally:
        om.close()
