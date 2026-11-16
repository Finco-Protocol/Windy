"""Calibration tests: insufficient-data fallback, factor training on
synthetic constant-ratio data, holdout metrics, derived-only application."""

import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.power import farm_power_mw, load_curve
from tests.conftest import make_raw_run

CURVE = load_curve()
PF = farm_power_mw(8.0, CURVE, 26, 0.0, 71.5)
RATIO = 1.2  # synthetic actual/forecast ratio


def _seed_hours(api, fake, n_hours, day0="2026-10-01"):
    """One archived GFS run covering n_hours hourly points + matching actuals
    at RATIO× the modelled energy."""
    start = datetime.fromisoformat(day0 + "T00:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(n_hours)]
    fake.push_run("gfs_seamless", f"{day0}T00:00", make_raw_run(times, {"80": [8.0] * n_hours, "100": [8.0] * n_hours}))
    resp = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day0, "run_hour": 0, "days": 16})
    assert resp.status_code == 200, resp.text

    rows = [(t, round(PF * RATIO, 4)) for t in times]
    csv_text = "ts,mwh\n" + "\n".join(f"{t}:00Z,{e}" for t, e in rows)
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    r = api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert r.status_code == 200 and r.json()["imported"] is True, r.text


def _range(n_hours, day0="2026-10-01"):
    start = datetime.fromisoformat(day0 + "T00:00:00+00:00")
    end = start + timedelta(hours=n_hours)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_calibration_insufficient_actuals_is_explicit(client):
    api, fake, _s = client
    _seed_hours(api, fake, 100)  # far below MIN_TRAIN/MIN_VAL
    f, t = _range(100)
    res = api.post("/api/calibration/train", json={"from_utc": f, "to_utc": t}).json()

    assert res["models"]["gfs_seamless"]["status"] == "not-calibrated-insufficient"
    cal = {c["model_id"]: c for c in api.get("/api/calibration").json()["models"]}
    assert cal["gfs_seamless"]["status"] == "not-calibrated-insufficient"
    assert cal["gfs_seamless"]["factor"] == 1.0
    assert "insufficient" in cal["gfs_seamless"]["note"]

    # accuracy stays uncalibrated even if asked
    acc = api.get("/api/accuracy", params={"from_utc": f, "to_utc": t, "use_calibration": True}).json()
    assert acc["calibration_applied"] == {}


def test_calibration_trains_bounded_factor_with_holdout(client):
    api, fake, settings = client
    n = 384  # 16 days × 24 h; lead-0 exclusion → 383 matched hours
    _seed_hours(api, fake, n)
    f, t = _range(n)

    conn = sqlite3.connect(settings.db_path)
    before = conn.execute(
        "SELECT modeled_power_mw, interval_energy_mwh FROM forecast_points ORDER BY valid_at_utc LIMIT 1"
    ).fetchone()
    conn.close()

    res = api.post("/api/calibration/train", json={"from_utc": f, "to_utc": t}).json()
    model = res["models"]["gfs_seamless"]
    assert model["status"] == "calibrated"
    assert model["factor"] == pytest.approx(RATIO, abs=0.01)
    assert model["n_val_hours"] >= 72
    assert model["val_mae_after"] < model["val_mae_before"]
    assert model["val_wape_after"] < model["val_wape_before"]

    conn = sqlite3.connect(settings.db_path)
    after = conn.execute(
        "SELECT modeled_power_mw, interval_energy_mwh FROM forecast_points ORDER BY valid_at_utc LIMIT 1"
    ).fetchone()
    conn.close()
    assert after == before, "training must never modify archived forecast values"

    # derived view applies the factor; the plain view does not
    plain = api.get("/api/accuracy", params={"from_utc": f, "to_utc": t}).json()
    calib = api.get("/api/accuracy", params={"from_utc": f, "to_utc": t, "use_calibration": True}).json()
    assert plain["calibration_applied"] == {}
    assert calib["calibration_applied"]["gfs_seamless"] == pytest.approx(RATIO, abs=0.01)
    p_tot = plain["models"]["gfs_seamless"]["total"]
    c_tot = calib["models"]["gfs_seamless"]["total"]
    assert p_tot["forecast_energy_mwh"] != c_tot["forecast_energy_mwh"]
    assert c_tot["mae_mw"] < p_tot["mae_mw"], "applying the trained factor must improve the synthetic holdout"


def test_calibration_factor_is_bounded(client):
    api, fake, _s = client
    from app import calibration as cal

    n = 384
    # ratio 2.0 is outside [0.5, 1.5] but stays within the site physical limit
    # (PF≈30.4 MW × 2 ≈ 60.8 MW < 71.5 MW) → factor clipped to FACTOR_MAX
    start = datetime.fromisoformat("2026-10-01T00:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(n)]
    fake.push_run("gfs_seamless", "2026-10-01T00:00", make_raw_run(times, {"80": [8.0] * n, "100": [8.0] * n}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-10-01", "run_hour": 0, "days": 16})
    rows = [(t, round(PF * 2.0, 4)) for t in times]  # ratio 2 → clipped to 1.5
    csv_text = "ts,mwh\n" + "\n".join(f"{t}:00Z,{e}" for t, e in rows)
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    f, t = _range(n)
    res = api.post("/api/calibration/train", json={"from_utc": f, "to_utc": t}).json()
    model = res["models"]["gfs_seamless"]
    assert model["status"] == "calibrated"
    assert model["factor"] == cal.FACTOR_MAX  # bounded — never an unbounded correction
