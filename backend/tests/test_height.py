"""Height resolution tests: log interpolation + proxy policy."""

import pytest

from app.height import HEIGHT_METHOD_LOG_INTERP, HEIGHT_METHOD_PROXY, log_interp_speed, resolve_hub_speed


def test_log_interp_known_value():
    # 6 m/s at 80 m and 8 m/s at 100 m → 90 m:
    # 6 + 2 * ln(90/80)/ln(100/80) ≈ 7.0557
    assert log_interp_speed(6.0, 80.0, 8.0, 100.0, 90.0) == pytest.approx(7.0557, abs=1e-3)


def test_log_interp_linear_in_log_space():
    import math

    # ln(90/80)/ln(120/80) ≈ 0.289 — NOT the linear midpoint; verify exactly:
    mid = log_interp_speed(5.0, 80.0, 9.0, 120.0, 90.0)
    expected = 5.0 + 4.0 * math.log(90 / 80) / math.log(120 / 80)
    assert mid == pytest.approx(expected, abs=1e-12)


def test_log_interp_bounds_and_errors():
    assert log_interp_speed(6.0, 80.0, 8.0, 100.0, 80.0) == pytest.approx(6.0)
    assert log_interp_speed(6.0, 80.0, 8.0, 100.0, 100.0) == pytest.approx(8.0)
    with pytest.raises(ValueError):
        log_interp_speed(6.0, 80.0, 8.0, 100.0, 120.0)  # not bracketed
    with pytest.raises(ValueError):
        log_interp_speed(6.0, 100.0, 8.0, 100.0, 90.0)  # same levels
    with pytest.raises(ValueError):
        log_interp_speed(6.0, -80.0, 8.0, 100.0, 90.0)


def test_resolve_proxy():
    speed, status = resolve_hub_speed({100.0: 7.2}, 90.0, HEIGHT_METHOD_PROXY)
    assert (speed, status) == (7.2, "ok")
    speed, status = resolve_hub_speed({100.0: None}, 90.0, HEIGHT_METHOD_PROXY)
    assert speed is None and status == "missing-height"


def test_resolve_log_interp_missing_level_is_not_fabricated():
    speed, status = resolve_hub_speed({80.0: None, 100.0: 7.0}, 90.0, HEIGHT_METHOD_LOG_INTERP)
    assert speed is None and status == "missing-height"


def test_resolve_log_interp_uses_bracket_order():
    # order of dict must not matter
    a = resolve_hub_speed({80.0: 6.0, 100.0: 8.0}, 90.0, HEIGHT_METHOD_LOG_INTERP)
    b = resolve_hub_speed({100.0: 8.0, 80.0: 6.0}, 90.0, HEIGHT_METHOD_LOG_INTERP)
    assert a == b
    assert a[1] == "ok"


def test_unknown_method_rejected():
    with pytest.raises(ValueError):
        resolve_hub_speed({80.0: 6.0, 100.0: 8.0}, 90.0, "linear-teleport")
