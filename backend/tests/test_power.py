"""Power model tests: cut-in/rated/cut-out, farm cap, energy integration, provenance."""

import pytest

from app.power import (
    CURVE_STATUS_DEMO,
    CURVE_STATUS_MANUFACTURER,
    PowerCurve,
    demo_curve_2p75,
    farm_power_mw,
    interval_energy_mwh,
    load_curve,
)


@pytest.fixture(scope="module")
def ge_curve() -> PowerCurve:
    return load_curve()


def test_loaded_curve_is_manufacturer_reference(ge_curve):
    # windpowerlib present → the GE103/2750 curve from its bundled OEDB snapshot
    assert ge_curve.status == CURVE_STATUS_MANUFACTURER
    assert ge_curve.cut_in_mps == pytest.approx(3.0)
    assert ge_curve.cut_out_mps == pytest.approx(25.0)
    assert ge_curve.rated_power_mw == pytest.approx(2.78, abs=0.01)
    assert len(ge_curve.points) == 51


def test_below_cut_in_is_zero(ge_curve):
    assert ge_curve.power_mw_per_turbine(0.0) == 0.0
    assert ge_curve.power_mw_per_turbine(2.9) == 0.0


def test_above_cut_out_is_zero(ge_curve):
    assert ge_curve.power_mw_per_turbine(25.01) == 0.0
    assert ge_curve.power_mw_per_turbine(40.0) == 0.0
    # at exactly the last point the curve value still applies
    assert ge_curve.power_mw_per_turbine(25.0) == pytest.approx(ge_curve.rated_power_mw)


def test_unknown_wind_is_none_not_zero(ge_curve):
    assert ge_curve.power_mw_per_turbine(None) is None


def test_farm_cap_715_mw_respected(ge_curve):
    # 26 × 2.78 MW (curve max) exceeds the 71.5 MW nameplate → must cap
    assert 26 * ge_curve.rated_power_mw > 71.5
    assert farm_power_mw(20.0, ge_curve, 26, 0.0, 71.5) == pytest.approx(71.5)


def test_farm_power_monotone_nonnegative(ge_curve):
    prev = 0.0
    v = 0.0
    while v <= 25.0:
        p = farm_power_mw(v, ge_curve, 26, 0.0, 71.5)
        assert p is not None and 0.0 <= p <= 71.5
        assert p >= prev - 1e-9
        prev = p
        v += 0.5


def test_loss_factor_scales_down(ge_curve):
    full = farm_power_mw(6.0, ge_curve, 26, 0.0, 71.5)
    lossy = farm_power_mw(6.0, ge_curve, 26, 0.1, 71.5)
    assert lossy == pytest.approx(full * 0.9, rel=1e-6)


def test_none_wind_propagates(ge_curve):
    assert farm_power_mw(None, ge_curve, 26, 0.0, 71.5) is None


def test_invalid_loss_factor_rejected(ge_curve):
    with pytest.raises(ValueError):
        farm_power_mw(6.0, ge_curve, 26, 1.0, 71.5)
    with pytest.raises(ValueError):
        farm_power_mw(6.0, ge_curve, 26, -0.1, 71.5)


def test_energy_is_power_times_interval():
    assert interval_energy_mwh(50.0, 1.0) == pytest.approx(50.0)
    assert interval_energy_mwh(50.0, 0.5) == pytest.approx(25.0)
    assert interval_energy_mwh(None, 1.0) is None
    with pytest.raises(ValueError):
        interval_energy_mwh(50.0, 0.0)


def test_demo_curve_is_explicitly_labelled():
    demo = demo_curve_2p75()
    assert demo.status == CURVE_STATUS_DEMO
    assert "NOT manufacturer data" in demo.provenance
    # cubic ramp behaves: zero at cut-in, rated at rated speed, zero beyond cut-out
    assert demo.power_mw_per_turbine(3.0) == pytest.approx(0.0, abs=1e-6)
    assert demo.power_mw_per_turbine(12.0) == pytest.approx(2.75)
    assert demo.power_mw_per_turbine(24.0) == pytest.approx(2.75)
    assert demo.power_mw_per_turbine(26.0) == 0.0
