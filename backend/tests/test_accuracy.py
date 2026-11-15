"""Accuracy tests: MAE/bias/WAPE over exact known values, lead bins, no leakage,
overlay alignment. Forecast power is derived from the same GE curve the app uses
so expected metrics are exact."""

import pytest

from app.power import farm_power_mw, load_curve
from tests.conftest import make_raw_run

CURVE = load_curve()
COUNT, LOSS, CAP = 26, 0.0, 71.5
PF = farm_power_mw(8.0, CURVE, COUNT, LOSS, CAP)  # constant 8 m/s hub wind


def _hours(day: str, n: int) -> list[str]:
    return [f"{day}T{h:02d}:00" for h in range(n)]


def _import_actuals(api, day: str, energy_by_hour: list[float]) -> None:
    import io

    csv_text = "ts,mwh\n" + "\n".join(
        f"{day}T{h:02d}:00:00Z,{e}" for h, e in enumerate(energy_by_hour)
    )
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    resp = api.post(
        "/api/actuals/import",
        files={"file": (f"{day}.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert resp.status_code == 200 and resp.json()["imported"] is True, resp.text


def test_mae_bias_wape_exact(client):
    api, fake, _settings = client
    n = 24
    times = _hours("2026-11-14", n)
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(times, {"80": [8.0] * n, "100": [8.0] * n}))
    resp = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0, "days": 2})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]

    # sanity: forecast energy per hour is PF * 1h
    detail = api.get(f"/api/runs/{run_id}").json()["points"]
    assert detail[0]["modeled_power_mw"] == pytest.approx(PF, rel=1e-6)

    # actuals: first 12 h = forecast + 5 MW, last 12 h = forecast - 3 MW
    deltas = [5.0] * 12 + [-3.0] * 12
    actual = [round(PF + d, 6) for d in deltas]
    _import_actuals(api, "2026-11-14", actual)

    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    gfs = acc["models"]["gfs_seamless"]
    total = gfs["total"]
    # valid 14T00 == issue time → lead 0 h → excluded by the leakage guard;
    # compared hours are 14T01..14T23 with deltas[1:]
    compared = deltas[1:]
    n = len(compared)  # 23
    assert total["n_pairs"] == n
    assert total["mae_mw"] == pytest.approx(sum(abs(d) for d in compared) / n, abs=1e-3)
    # bias = mean(forecast − actual) (meteorological convention) = −mean(d)
    assert total["bias_mw"] == pytest.approx(-sum(compared) / n, abs=1e-3)
    abs_dev = sum(abs(d) for d in compared)
    sum_actual = sum(actual[1:])
    assert total["wape_energy"] == pytest.approx(abs_dev / sum_actual, abs=1e-3)
    assert total["coverage"] == pytest.approx(n / 24, abs=1e-3)  # API rounds to 4 decimals
    assert total["actual_energy_mwh"] == pytest.approx(sum_actual, abs=0.5)

    # all leads are 1..23 h → everything lands in the day-ahead bin
    assert set(gfs["bins"].keys()) == {"day-ahead (0-24h)"}
    assert gfs["bins"]["day-ahead (0-24h)"]["n_pairs"] == n


def test_no_leakage_before_issue(client):
    api, fake, _settings = client
    n = 6
    times = _hours("2026-11-13", n)  # run issued 14T00 must not score 13th hours
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(times, {"80": [8.0] * n, "100": [8.0] * n}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0})
    _import_actuals(api, "2026-11-13", [50.0] * n)

    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-13T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    # all pairs have valid_at <= issue → excluded
    assert acc["models"].get("gfs_seamless") in (None, {"bins": {}, "total": None})


def test_lead_bins_split(client):
    api, fake, _settings = client
    n24 = 24
    times_d13 = _hours("2026-11-13", 24) + _hours("2026-11-14", 24)  # 48 h from 13T00
    fake.push_run("gfs_seamless", "2026-11-13T00:00", make_raw_run(times_d13, {"80": [8.0] * 48, "100": [8.0] * 48}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-13", "run_hour": 0, "days": 3})

    actual = [round(PF, 6)] * 24
    _import_actuals(api, "2026-11-14", actual)

    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    bins = acc["models"]["gfs_seamless"]["bins"]
    # 14T00..14T23 relative to issue 13T00 → leads 24..47 → all in the 2-day bin
    assert bins["2-day ahead (24-48h)"]["n_pairs"] == 24
    assert "day-ahead (0-24h)" not in bins


def test_overlay_aligns_same_valid_time(client):
    api, fake, _settings = client
    n = 6
    times = _hours("2026-11-14", n)
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(times, {"80": [8.0] * n, "100": [8.0] * n}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0})
    _import_actuals(api, "2026-11-14", [round(PF, 6)] * n)

    ov = api.get(
        "/api/accuracy/overlay",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-14T06:00:00Z"},
    ).json()
    assert len(ov["hours"]) == 6
    # valid 14T00 == issue → lead 0 excluded; 14T01 onward has the forecast pair
    assert "models" not in ov["hours"][0]
    h1 = ov["hours"][1]
    assert h1["actual_mwh"] == pytest.approx(PF, rel=1e-3)
    assert h1["models"]["gfs_seamless"] == pytest.approx(PF, rel=1e-3)


def test_empty_accuracy_is_explicit(client):
    api, _fake, _settings = client
    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    assert acc["models"] == {}
    assert acc["actual_hours_in_range"] == 0
