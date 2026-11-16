"""P0 continuity regression: window completeness uses UTC interval union
duration, not record count.

- 24 × 30-min contiguous rows → only 12/24 h coverage → NOT complete.
- 24 × 1 h with a one-hour hole → NOT complete (subtotal reported).
- 24 × 1 h continuous → complete.
- 23 h / 25 h archived UTC days behave correctly inside 24 h windows.
- Legitimate zero production fully covered is complete 0 MWh.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import make_raw_run

DAY = "2026-11-13"


def _times(hours: int, day: str = DAY):
    start = datetime.fromisoformat(day + "T00:00:00+00:00")
    return [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(hours)]


def _times_min(start: datetime, n: int, step_min: int):
    return [(start + timedelta(minutes=i * step_min)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]


def _push_and_backfill(api, fake, times, ws=8.0, null_idx=None):
    arrays = {"80": [None if i in (null_idx or []) else ws for i in range(len(times))],
              "100": [None if i in (null_idx or []) else ws for i in range(len(times))]}
    fake.push_run("gfs_seamless", times[0], make_raw_run(times, arrays))
    day = times[0][:10]
    hour = int(times[0][11:13])
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day, "run_hour": hour, "days": 16, "site_id": "farm-a"})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _ensure_farm(api):
    r = api.post("/api/assets", json={
        "id": "farm-a", "label": "A", "latitude": 45.0, "longitude": 16.0, "capacity_mw": 71.5,
        "turbine_count": 26, "turbine_rated_mw": 2.75, "hub_height_m": 90,
        "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    })
    assert r.status_code == 200, r.text
    g = api.post("/api/assets/farm-a/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    assert g.status_code == 200, g.text


def _w24(api, run_id):
    return api.get("/api/assets/farm-a/scenario", params={"run_id": run_id}).json()["summary"]["windows"]["24h"]


def test_24x30min_contiguous_is_only_12h_not_complete(client):
    """24 rows of 30 minutes = 12 hours of coverage — record count must NOT
    make a 24 h window complete."""
    api, fake, _s = client
    _ensure_farm(api)
    times = _times_min(datetime.fromisoformat(DAY + "T00:00:00+00:00"), 24, 30)
    run_id = _push_and_backfill(api, fake, times, ws=8.0)

    w = _w24(api, run_id)
    assert w["complete"] is False
    assert w["valid_hours"] == pytest.approx(12.0, abs=1e-6)
    assert w["missing_hours"] == pytest.approx(12.0, abs=1e-3)
    assert w["coverage"] == pytest.approx(0.5)
    assert w["adjusted_mwh"] is None, "full-window total must be null for a half-covered window"
    assert w["available_hours_subtotal"]["hours"] == pytest.approx(12.0, abs=1e-6)
    assert w["n_valid_intervals"] == 24


def test_hole_makes_window_incomplete(client):
    api, fake, _s = client
    _ensure_farm(api)
    times = [t for i, t in enumerate(_times(24)) if i != 5]  # hole at hour 05
    run_id = _push_and_backfill(api, fake, times, ws=8.0)

    w = _w24(api, run_id)
    assert w["complete"] is False, "a hole must keep the window incomplete"
    assert w["missing_hours"] == pytest.approx(1.0, abs=1e-3)
    assert w["adjusted_mwh"] is None
    assert w["available_hours_subtotal"]["hours"] == pytest.approx(23.0, abs=1e-6)


def test_24x1h_continuous_is_complete(client):
    api, fake, _s = client
    _ensure_farm(api)
    run_id = _push_and_backfill(api, fake, _times(24), ws=8.0)
    w = _w24(api, run_id)
    assert w["complete"] is True
    assert w["valid_hours"] == pytest.approx(24.0, abs=1e-6)
    assert w["adjusted_mwh"] is not None and w["adjusted_mwh"] > 0


def test_dst_23h_and_25h_utc_days(client):
    api, fake, _s = client
    _ensure_farm(api)
    # 23 archived UTC hours issued 18:00Z (covers the 23 h local spring day
    # 2026-03-29) — a 24 h window cannot complete, but the 23 h union is exact
    spring_start = datetime(2026, 3, 28, 18, 0, tzinfo=timezone.utc)
    times23 = [(spring_start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(23)]
    run_id = _push_and_backfill(api, fake, times23, ws=8.0)
    w = _w24(api, run_id)
    assert w["complete"] is False, "23 archived hours cannot complete a 24 h window"
    assert w["valid_hours"] == pytest.approx(23.0, abs=1e-6)

    # 25 archived UTC hours issued 2026-10-24T18:00Z (the 25 h local fall-back
    # day): a 24 h window over the continuous union completes normally
    fall_start = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
    times25 = [(fall_start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(25)]
    run_id25 = _push_and_backfill(api, fake, times25, ws=8.0)
    w25 = _w24(api, run_id25)
    assert w25["complete"] is True, "a 24 h UTC window inside a 25 h archived day completes normally"
    assert w25["valid_hours"] == pytest.approx(24.0, abs=1e-6)


def test_two_hour_gap_22h_valid_null_total_with_subtotal(client):
    api, fake, _s = client
    _ensure_farm(api)
    times = [t for t in _times(24) if not (DAY + "T05:00" <= t < DAY + "T07:00")]  # 2 h hole
    run_id = _push_and_backfill(api, fake, times, ws=8.0)

    w = _w24(api, run_id)
    assert w["complete"] is False
    assert w["adjusted_mwh"] is None
    assert w["valid_hours"] == pytest.approx(22.0, abs=1e-6)
    assert w["available_hours_subtotal"]["hours"] == pytest.approx(22.0, abs=1e-6)
    assert w["available_hours_subtotal"]["adjusted_mwh"] is not None


def test_zero_production_fully_covered_is_complete_zero(client):
    api, fake, _s = client
    _ensure_farm(api)
    run_id = _push_and_backfill(api, fake, _times(24), ws=0.0)
    w = _w24(api, run_id)
    assert w["complete"] is True
    assert w["adjusted_mwh"] == 0.0
