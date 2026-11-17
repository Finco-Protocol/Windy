"""PR#8 Correction A §1 — P0 MWh math regressions.

Fixture "cap10": 2 x 5.0 MW units, capacity 10.0 MW (installed == capacity),
loss 0, scaled-reference opt-in. At high wind BOTH the archived baseline and
the scenario-adjusted value are EXACTLY 10.0 MW, so every identity below is
checked against literal owner numbers:

- a 24 x 1 h day at 10 MW  = 240 MWh (split across two local dates)
- a 3 h interval at 10 MW  =  30 MWh, crossing local midnight as 10 + 20 MWh
- a 23 h spring DST day    = 230 MWh (hours_expected 23, not partial)
- a 25 h fall DST day      = 250 MWh (hours_expected 25, not partial)
- missing wind hours       -> hours_missing, never fabricated 0 MWh
- met-only model           -> adjusted unavailable in EVERY mode
- incompatible curve       -> adjusted FAILS CLEARLY (no silent nulls)
- baseline archive + baseline daily_local remain byte-identical
"""

from datetime import datetime, timedelta

import pytest

from tests.conftest import make_raw_run
from fastapi.testclient import TestClient  # noqa: F401

WS = 15.0  # high wind -> curve saturated -> farm clamps at capacity


def _mk_site(api, site_id="cap10"):
    r = api.post(
        "/api/assets",
        json={
            "id": site_id,
            "label": "10 MW fixture",
            "latitude": 45.0,
            "longitude": 16.0,
            "capacity_mw": 10.0,
            "turbine_count": 4,
            "turbine_rated_mw": 2.5,
            "hub_height_m": 90,
            "loss_factor": 0.0,
            "timezone": "Europe/Podgorica",
            "power_curve_id": "ge103-2750-oedb",
            "allow_scaled_reference": True,
        },
    )
    assert r.status_code == 200, r.text
    g = api.post(f"/api/assets/{site_id}/turbines/generate", json={"count": 4, "rating_mw": 2.5})
    assert g.status_code == 200, g.text
    assert g.json()["installed_mw"] == pytest.approx(10.0, abs=1e-6)


def _run(api, fake, site_id, run_day, times, wind=WS):
    """Archive one gfs run whose hourly times may start at any UTC hour."""
    n = len(times)
    fake.push_run("gfs_seamless", f"{run_day}T00:00", make_raw_run(times, {"80": [wind] * n, "100": [wind] * n}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": run_day, "run_hour": 0, "days": 16, "site_id": site_id})
    assert r.status_code == 200, r.text


def _adjusted_daily(api, site_id="cap10", days=7):
    r = api.get("/api/forecast", params={"days": days, "site_id": site_id, "with_availability": "true"})
    assert r.status_code == 200, r.text
    fc = r.json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    adj = m["adjusted"]
    assert adj["available"] is True, f"adjusted unavailable: {adj.get('reason')}"
    return adj


def _hourly(day, start_h, n):
    t0 = datetime.fromisoformat(f"{day}T{start_h:02d}:00:00+00:00")
    return [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(n)]


def test_24h_at_10mw_is_240mwh_across_two_local_dates(client):
    api, fake, _s = client
    _mk_site(api)
    # 24 hourly points 12:00Z day0 -> 11:00Z day1; local midnight = 23:00Z (CET)
    _run(api, fake, "cap10", "2026-11-16", _hourly("2026-11-16", 12, 24))
    adj = _adjusted_daily(api)
    rows = {r["date"]: r for r in adj["daily"]}
    # regression for the return-inside-loop P0 bug: BOTH dates present with
    # full sums (11 h + 13 h), never just the first point / first date
    assert set(rows) == {"2026-11-16", "2026-11-17"}
    assert rows["2026-11-16"]["mwh"] == pytest.approx(11 * 10.0, abs=1e-3), "11 h x 10 MW = 110 MWh"
    assert rows["2026-11-17"]["mwh"] == pytest.approx(13 * 10.0, abs=1e-3), "13 h x 10 MW = 130 MWh"
    assert sum(r["mwh"] for r in adj["daily"]) == pytest.approx(240.0, abs=1e-2), "24 h at 10 MW = 240 MWh"
    assert rows["2026-11-16"]["hours_counted"] == pytest.approx(11.0)
    assert rows["2026-11-16"]["partial"] is True  # day shown is incomplete


def test_3h_interval_at_10mw_is_30mwh_split_across_midnight(client):
    api, fake, _s = client
    _mk_site(api)
    # 3 h spacing: intervals (after the 1 h first-interval artifact):
    # [16,17) D0 1 h, [19,22) D0 3 h, [22,01) crossing local midnight
    # (23:00Z) -> 1 h x 10 MW in D0 + 2 h x 10 MW in D1 = 30 MWh total
    times = ["2026-11-16T16:00", "2026-11-16T19:00", "2026-11-16T22:00"]
    _run(api, fake, "cap10", "2026-11-16", times)
    adj = _adjusted_daily(api)
    rows = {r["date"]: r for r in adj["daily"]}
    assert rows["2026-11-16"]["mwh"] == pytest.approx(10.0 * (1 + 3 + 1), abs=1e-3), "50 MWh in D0"
    assert rows["2026-11-17"]["mwh"] == pytest.approx(10.0 * 2, abs=1e-3), "20 MWh in D1; crossing interval = 30 MWh total"
    assert rows["2026-11-16"]["hours_counted"] == pytest.approx(5.0)
    assert rows["2026-11-17"]["hours_counted"] == pytest.approx(2.0)


def test_spring_dst_23h_day_is_230mwh_not_partial(client):
    api, fake, _s = client
    _mk_site(api)
    # local day 2026-03-29 spans [2026-03-28T23:00Z, 2026-03-29T22:00Z) = 23 h
    _run(api, fake, "cap10", "2026-03-29", _hourly("2026-03-28", 23, 23))
    adj = _adjusted_daily(api)
    rows = {r["date"]: r for r in adj["daily"]}
    assert rows["2026-03-29"]["hours_expected"] == pytest.approx(23.0)
    assert rows["2026-03-29"]["hours_counted"] == pytest.approx(23.0)
    assert rows["2026-03-29"]["mwh"] == pytest.approx(230.0, abs=1e-2)
    assert rows["2026-03-29"]["partial"] is False


def test_fall_dst_25h_day_is_250mwh_not_partial(client):
    api, fake, _s = client
    _mk_site(api)
    # local day 2026-10-25 spans [2026-10-24T22:00Z, 2026-10-25T23:00Z) = 25 h
    _run(api, fake, "cap10", "2026-10-24", _hourly("2026-10-24", 22, 25))
    adj = _adjusted_daily(api)
    rows = {r["date"]: r for r in adj["daily"]}
    assert rows["2026-10-25"]["hours_expected"] == pytest.approx(25.0)
    assert rows["2026-10-25"]["hours_counted"] == pytest.approx(25.0)
    assert rows["2026-10-25"]["mwh"] == pytest.approx(250.0, abs=1e-2)
    assert rows["2026-10-25"]["partial"] is False


def test_missing_wind_hours_stay_missing_never_zero(client):
    api, fake, _s = client
    _mk_site(api)
    times = _hourly("2026-11-16", 0, 24)
    raw = make_raw_run(times, {"80": [WS] * 24, "100": [WS] * 24})
    raw["hourly"]["wind_speed_80m"][5] = None  # two intervals unresolved
    raw["hourly"]["wind_speed_100m"][5] = None
    raw["hourly"]["wind_speed_80m"][9] = None
    raw["hourly"]["wind_speed_100m"][9] = None
    fake.push_run("gfs_seamless", "2026-11-16T00:00", raw)
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-16", "run_hour": 0, "days": 16, "site_id": "cap10"})
    assert r.status_code == 200, r.text
    adj = _adjusted_daily(api)
    rows = {r["date"]: r for r in adj["daily"]}
    # local midnight is 23:00Z: intervals [00,23) belong to 11-16 (23 h,
    # two of them missing), [23,00) belongs to 11-17
    d0 = rows["2026-11-16"]
    assert d0["hours_counted"] == pytest.approx(21.0)
    # 24-h expected day: 21 covered locally, 2 unresolved + 1 hour that fell
    # into the next local date = 3 explicit missing hours for this day
    assert d0["hours_missing"] == pytest.approx(3.0)
    assert d0["mwh"] == pytest.approx(210.0, abs=1e-2), "only covered hours count; missing hours never filled with 0"
    assert d0["partial"] is True
    d1 = rows["2026-11-17"]
    assert d1["hours_counted"] == pytest.approx(1.0)
    assert d1["hours_missing"] == pytest.approx(23.0)
    assert d1["mwh"] == pytest.approx(10.0, abs=1e-2)
    assert d1["partial"] is True


def test_met_only_model_never_gets_adjusted_mwh(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    h24 = [f"2026-11-16T{h:02d}:00" for h in range(24)]
    fake.push_run("ukmo_seamless", "2026-11-16T00:00", make_raw_run(h24, {"10": [12.0] * 24}))
    r = api.post("/api/backfill", json={"model_id": "ukmo_seamless", "run_date": "2026-11-16", "run_hour": 0, "days": 7})
    assert r.status_code == 200, r.text
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "ukmo_seamless")
    assert m["adjusted"]["available"] is False
    assert "met-only" in m["adjusted"]["reason"]
    assert m["adjusted"]["daily"] == [] and m["adjusted"]["points"] == []
    # archived points still carry wind but never power
    assert all(p["modeled_power_mw"] is None for p in m["points"])


def test_incompatible_curve_fails_clearly_no_scaled_optin(client):
    api, fake, _s = client
    # 5.0 MW units on the 2.78 MW reference curve WITHOUT the scaled opt-in
    r = api.post(
        "/api/assets",
        json={
            "id": "badcurve",
            "label": "curve mismatch fixture",
            "latitude": 45.0,
            "longitude": 16.0,
            "capacity_mw": 10.0,
            "turbine_count": 4,
            "turbine_rated_mw": 2.5,
            "hub_height_m": 90,
            "loss_factor": 0.0,
            "timezone": "Europe/Podgorica",
            "power_curve_id": "ge103-2750-oedb",
            "allow_scaled_reference": False,
        },
    )
    assert r.status_code == 200, r.text
    api.post("/api/assets/badcurve/turbines/generate", json={"count": 4, "rating_mw": 2.5})
    _run(api, fake, "badcurve", "2026-11-16", _hourly("2026-11-16", 0, 24))
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "badcurve", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    assert m["adjusted"]["available"] is False, "incompatible curve must fail clearly, not silently null"
    assert "incompatible-curve" in m["adjusted"]["reason"]
    assert m["adjusted"]["daily"] == []


def test_baseline_archives_untouched_by_availability_mode(client):
    api, fake, _s = client
    _mk_site(api)
    _run(api, fake, "cap10", "2026-11-16", _hourly("2026-11-16", 0, 24))
    base = api.get("/api/forecast", params={"days": 2, "site_id": "cap10"}).json()
    adj = api.get("/api/forecast", params={"days": 2, "site_id": "cap10", "with_availability": "true"}).json()
    mb = next(x for x in base["models"] if x["model_id"] == "gfs_seamless")
    ma = next(x for x in adj["models"] if x["model_id"] == "gfs_seamless")
    assert mb["points"] == ma["points"], "archived baseline points identical in both modes"
    assert mb["daily_local"] == ma["daily_local"], "baseline daily rows identical in both modes"
    assert ma["points"][0]["modeled_power_mw"] == pytest.approx(10.0, abs=1e-6)
    run_detail = api.get(f"/api/runs/{mb['run']['id']}", params={"site_id": "cap10"}).json()
    assert run_detail["points"][0]["modeled_power_mw"] == pytest.approx(10.0, abs=1e-6)
