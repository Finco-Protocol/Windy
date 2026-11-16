"""P0-3 regression: scenario window coverage semantics.

Full-window totals are null/not-comparable unless the window is COMPLETE
(computed hours == window hours); available-hours subtotals are separate;
legitimate zero production stays distinct from missing data; 10-day windows
flag short-horizon models (ICON-EU 120 h vs ECMWF 240 h)."""

from datetime import datetime, timedelta, timezone

from tests.conftest import make_raw_run

DAY = "2026-11-14"


def _ensure_site(api, site_id="farm-a", **over):
    body = {
        "id": site_id, "label": "Farm", "latitude": 45.0, "longitude": 16.0,
        "capacity_mw": 71.5, "turbine_count": 26, "turbine_rated_mw": 2.75,
        "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    }
    body.update(over)
    r = api.post("/api/assets", json=body)
    if r.status_code == 200:
        # new site: generate its inventory from the declared configuration
        g = api.post(f"/api/assets/{site_id}/turbines/generate", json={"count": body["turbine_count"], "rating_mw": body["turbine_rated_mw"]})
        assert g.status_code == 200, g.text
    else:
        # already exists (idempotent helper): keep its existing inventory —
        # never regenerate a different fleet over it
        assert "already exists" in r.text, r.text


def _backfill_day(api, fake, model_id, day, hours, ws_by_height, null_first=0, ws=None, site_id="farm-a"):
    _ensure_site(api, site_id=site_id)
    start = datetime.fromisoformat(day + "T00:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(hours)]
    arrays = {}
    for h_name, vals in ws_by_height.items():
        arr = list(vals[:hours]) if ws is None else [ws] * hours
        for i in range(min(null_first, hours)):
            arr[i] = None
        arrays[h_name] = arr
    fake.push_run(model_id, f"{day}T00:00", make_raw_run(times, arrays))
    r = api.post(
        "/api/backfill",
        json={"model_id": model_id, "run_date": day, "run_hour": 0, "days": 16, "site_id": site_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _windows(api, run_id, site_id="farm-a"):
    sc = api.get(f"/api/assets/{site_id}/scenario", params={"run_id": run_id}).json()
    return sc["summary"]["windows"], sc["points"]


def test_24_complete_hours_window_is_complete(client):
    api, fake, _s = client
    run_id = _backfill_day(api, fake, "gfs_seamless", "2026-11-13", 24, {"80": [8.0] * 24, "100": [8.0] * 24})
    w, _pts = _windows(api, run_id)

    w24 = w["24h"]
    assert w24["complete"] is True
    assert w24["valid_hours"] == 24 and w24["missing_hours"] == 0
    assert w24["coverage"] == 1.0
    assert w24["adjusted_mwh"] is not None and w24["adjusted_mwh"] > 0


def test_one_missing_wind_hour_makes_window_incomplete(client):
    api, fake, _s = client
    run_id = _backfill_day(api, fake, "gfs_seamless", "2026-11-13", 24, {"80": [8.0] * 24, "100": [8.0] * 24}, null_first=1)
    w, pts = _windows(api, run_id)

    w24 = w["24h"]
    assert w24["complete"] is False
    assert w24["missing_hours"] == 1
    assert w24["adjusted_mwh"] is None, "incomplete window must not pretend gaps are zero"
    sub = w24["available_hours_subtotal"]
    assert sub["hours"] == 23
    assert sub["adjusted_mwh"] is not None
    assert sub["note"].startswith("subtotal over covered hours only")
    # the missing hour itself is a null point, never a fabricated zero
    first = next(p for p in pts if p["valid_at_utc"] == "2026-11-13T00:00:00Z")
    assert first["status"] == "missing-wind" and first["adjusted_mw"] is None


def test_all_zero_wind_fully_covered_is_complete_zero(client):
    api, fake, _s = client
    run_id = _backfill_day(api, fake, "gfs_seamless", "2026-11-13", 24, {"80": [0.0] * 24, "100": [0.0] * 24})
    w, _pts = _windows(api, run_id)

    w24 = w["24h"]
    assert w24["complete"] is True
    assert w24["adjusted_mwh"] == 0.0, "zero production is a real measured/derived value, not missing"


def test_incompatible_curve_hours_are_missing_for_window(client):
    api, fake, _s = client
    # site with 3.0 MW units on the 2.78-max GE curve without scaling opt-in
    _ensure_site(api, site_id="farm-c", id="farm-c", label="C", capacity_mw=30, turbine_count=10, turbine_rated_mw=3.0)
    run_id = _backfill_day(api, fake, "gfs_seamless", "2026-11-13", 24, {"80": [8.0] * 24, "100": [8.0] * 24}, site_id="farm-c")

    w, pts = _windows(api, run_id, site_id="farm-c")
    assert all(p["status"] == "incompatible-curve" for p in pts)
    assert w["24h"]["complete"] is False and w["24h"]["adjusted_mwh"] is None
    assert w["24h"]["available_hours_subtotal"]["hours"] == 0


def test_10d_window_flags_short_horizon_icon(client):
    api, fake, _s = client
    # ECMWF and ICON-EU runs, 96 h archived each — same query window
    run_e = _backfill_day(api, fake, "ecmwf_ifs", "2026-11-13", 96, {"100": [8.0] * 96})
    run_i = _backfill_day(api, fake, "icon_eu", "2026-11-13", 96, {"80": [8.0] * 96, "120": [8.0] * 96})

    we, _ = _windows(api, run_e)
    wi, _ = _windows(api, run_i)
    # both runs archive only 96 h → 7d/10d windows incomplete with short_horizon
    assert we["7d"]["complete"] is False and we["7d"]["missing_hours"] == 72
    assert we["10d"]["short_horizon"] is True, "ECMWF registry horizon is 240 h but this run archived only 96 h"
    assert wi["10d"]["short_horizon"] is True, "ICON-EU horizon is 120 h — never a 10-day total"
    assert wi["7d"]["short_horizon"] is True
    assert wi["7d"]["adjusted_mwh"] is None
    assert wi["7d"]["available_hours_subtotal"]["hours"] == 96


def test_curtailment_with_missing_rows_totals(client):
    api, fake, _s = client
    run_id = _backfill_day(api, fake, "gfs_seamless", "2026-11-13", 24, {"80": [8.0] * 24, "100": [8.0] * 24}, null_first=2)
    api.post("/api/assets/farm-a/constraints", json={"rows": [
        {"kind": "export_cap", "cap_mw": 30, "start_local": "2026-11-12T00:00", "end_local": None}
    ]})
    w, _pts = _windows(api, run_id)

    w24 = w["24h"]
    assert w24["complete"] is False and w24["adjusted_mwh"] is None
    sub = w24["available_hours_subtotal"]
    # 22 computed hours, each capped at 30 MW → 22 × 30
    assert sub["hours"] == 22
    assert sub["adjusted_mwh"] == 22 * 30.0
    assert sub["lost_export_mwh"] > 0
