"""Interval-alignment tests: 15/30/60-minute meter data, partial hours,
unaligned intervals, and DST 23/25-hour local days."""

import io

from tests.conftest import make_raw_run


def _import_rows(api, rows, interval_hours, tz="UTC"):
    """rows: [(start "YYYY-MM-DDTHH:MM", energy)] — interval-average MW/energy per row."""
    csv_text = "ts,mwh\n" + "\n".join(f"{s}:00Z,{e}" for s, e in rows)
    mapping = (
        '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh",'
        f'"tz":"{tz}","interval_hours":{interval_hours}}}'
    )
    resp = api.post(
        "/api/actuals/import",
        files={"file": ("m.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert resp.status_code == 200 and resp.json()["imported"] is True, resp.text


def _push_hourly_forecast(api, fake, day, n_hours, ws=8.0):
    times = [f"{day}T{h:02d}:00" for h in range(n_hours)]
    fake.push_run("gfs_seamless", f"{day}T00:00", make_raw_run(times, {"80": [ws] * n_hours, "100": [ws] * n_hours}))
    resp = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day, "run_hour": 0, "days": 16})
    assert resp.status_code == 200, resp.text


def _acc(api, f, t):
    return api.get("/api/accuracy", params={"from_utc": f, "to_utc": t}).json()


# ---- sub-hourly aggregation ----------------------------------------------------


def test_15min_actuals_aggregate_to_complete_hours(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 3)
    rows = []
    for h in range(3):
        for q in range(4):  # 4×15 min = full hour, 1 MWh each → 4 MWh/h
            rows.append((f"2026-11-14T{h:02d}:{q * 15:02d}", 1))
    _import_rows(api, rows, 0.25)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T03:00:00Z")
    # hour 0 has lead 0 (excluded); hours 1-2 pair
    assert acc["actual_hours_in_range"] == 3
    gfs = acc["models"]["gfs_seamless"]
    assert gfs["total"]["n_pairs"] == 2
    assert gfs["total"]["actual_energy_mwh"] == 8.0  # 2 h × 4 MWh


def test_partial_hour_excluded_and_reported(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 3)
    rows = []
    for h in range(3):
        for q in range(4 if h < 2 else 3):  # last hour only 3/4 covered
            rows.append((f"2026-11-14T{h:02d}:{q * 15:02d}", 1))
    _import_rows(api, rows, 0.25)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T03:00:00Z")
    assert acc["actual_hours_in_range"] == 2  # complete hours only
    assert acc["actual_hours_partial"] == 1
    gfs = acc["models"]["gfs_seamless"]
    assert gfs["total"]["n_pairs"] == 1, "partial hour must never be scored"


def test_30min_actuals_aggregate(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 2)
    rows = []
    for h in range(2):
        rows.append((f"2026-11-14T{h:02d}:00", 2))
        rows.append((f"2026-11-14T{h:02d}:30", 2))
    _import_rows(api, rows, 0.5)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z")
    assert acc["actual_hours_in_range"] == 2
    assert acc["models"]["gfs_seamless"]["total"]["actual_energy_mwh"] == 4.0  # hour 1 only


def test_boundary_straddling_interval_is_unaligned(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 3)
    rows = [("2026-11-14T00:00", 1), ("2026-11-14T01:00", 1)]
    rows.append(("2026-11-14T00:10", 1))  # 00:10→01:10 straddles the boundary
    _import_rows(api, rows, 1.0)  # declared 1h but the interval crosses hours

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T03:00:00Z")
    assert acc["actual_intervals_unaligned"] == 1
    # hours 0 and 1 keep only their aligned interval → complete anyway (1×1h);
    # the straddler contributes nothing
    assert acc["actual_hours_in_range"] == 2
    assert acc["models"]["gfs_seamless"]["total"]["actual_energy_mwh"] == 1.0  # hour 1 = 1 MWh


# ---- DST days: UTC grouping is inherently DST-safe ------------------------------


def test_dst_spring_23h_and_fall_25h_local_days(client):
    api, _fake, _s = client
    from datetime import datetime, timedelta, timezone

    # Local 2026-03-29 (Europe/Podgorica) = 23 UTC hours; 2026-10-25 = 25 UTC hours.
    spring = [datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(23)]
    fall = [datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(25)]

    def fmt(dt):
        return dt.strftime("%Y-%m-%dT%H:%M")

    _import_rows(api, [(fmt(dt), 1) for dt in spring], 1.0)
    _import_rows(api, [(fmt(dt), 1) for dt in fall], 1.0)

    acc_spring = _acc(api, "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z")
    acc_fall = _acc(api, "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z")
    assert acc_spring["actual_hours_in_range"] == 23, "local spring-forward day = 23 complete UTC hours"
    assert acc_fall["actual_hours_in_range"] == 25, "local fall-back day = 25 complete UTC hours"
    assert acc_spring["actual_hours_partial"] == 0 and acc_fall["actual_hours_partial"] == 0


def test_hourly_energy_equals_average_mw_for_complete_hour(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 2)
    _import_rows(api, [("2026-11-14T00:00", 5)], 1.0)
    _import_rows(api, [("2026-11-14T01:00", 7)], 1.0)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z")
    gfs = acc["models"]["gfs_seamless"]["total"]
    assert gfs["actual_energy_mwh"] == 7.0  # only hour 1 pairs (lead > 0)
    assert gfs["n_pairs"] == 1
