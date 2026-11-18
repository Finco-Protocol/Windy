"""Correction E §3 — GEM root cause: upstream returns 80 m and 120 m wind
(live probe 2026-11-18: 240/240 non-null across the 10-day horizon) while
100 m is ALL NULL on every GEM id variant. The registry now requests 80/120
with log-interp; archived 100 m runs keep their missing-height statuses."""

from datetime import datetime, timedelta

import pytest

from app.collect import build_points
from app.registry import get_model
from tests.conftest import make_raw_run

DAY = "2026-11-18"


def _times(n=24):
    t0 = datetime.fromisoformat(f"{DAY}T00:00:00+00:00")
    return [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(n)]


def test_gem_registry_requests_80_120_not_null_100():
    spec = get_model("gem_seamless")
    assert spec.speed_heights == (80, 120), "live probe: 100 m ALL NULL, 80/120 fully populated"
    assert spec.height_method == "log-interp"
    assert 100 not in spec.speed_heights


def test_gem_build_points_interpolates_hub_from_80_120():
    spec = get_model("gem_seamless")
    from app.power import load_curve

    curve = load_curve()
    raw = make_raw_run(_times(), {"80": [4.0] * 24, "120": [6.0] * 24})
    rows = build_points(raw, spec, 90, curve, 26, 0.0, 74.1)
    assert len(rows) == 24
    assert all(r.wind_speed_mps is not None for r in rows), "hub wind resolved from 80/120 — never missing-height"
    # log-interp between 4.0 and 6.0 at 90 m: strictly between the two
    hub = rows[0].wind_speed_mps
    assert 4.0 < hub < 6.0
    assert all(r.status == "ok" for r in rows)


def test_gem_partial_null_heights_stay_missing_not_zero():
    spec = get_model("gem_seamless")
    from app.power import load_curve

    curve = load_curve()
    raw = make_raw_run(_times(3), {"80": [4.0, None, 4.0], "120": [6.0, 6.0, None]})
    rows = build_points(raw, spec, 90, curve, 26, 0.0, 74.1)
    assert rows[0].wind_speed_mps is not None and rows[0].status == "ok"
    assert rows[1].wind_speed_mps is None and rows[1].status == "missing-height"
    assert rows[2].wind_speed_mps is None and rows[2].status == "missing-height"
    assert rows[1].modeled_power_mw is None, "missing wind never becomes 0 MWh"
