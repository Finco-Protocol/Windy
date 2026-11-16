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


def test_boundary_straddling_interval_marks_hour_unscoreable(client):
    api, fake, _s = client
    _push_hourly_forecast(api, fake, "2026-11-14", 3)
    rows = [("2026-11-14T00:00", 1), ("2026-11-14T01:00", 1)]
    rows.append(("2026-11-14T00:10", 1))  # 00:10→01:10 straddles the boundary
    _import_rows(api, rows, 1.0)  # declared 1h but the interval crosses hours

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T03:00:00Z")
    assert acc["actual_intervals_unaligned"] == 1
    # V1.2 stricter rule: an hour containing a boundary-crossing interval is
    # NOT scoreable (reported with a reason) even if another aligned interval
    # would have covered it — no quiet fixing of untrusted source data.
    assert acc["actual_hours_in_range"] == 1  # hour 1 only
    ov = api.get("/api/accuracy/overlay", params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-14T03:00:00Z"}).json()
    h0 = next(h for h in ov["hours"] if h["valid_at_utc"] == "2026-11-14T00:00:00Z")
    assert h0.get("actual_partial") is True
    assert h0.get("actual_reason") == "boundary-crossing interval"
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


# ---- V1.2 strict coverage: overlap / gap / mixed / limits -----------------------


def acc_overlay_hours(api, f, t):
    return api.get("/api/accuracy/overlay", params={"from_utc": f, "to_utc": t}).json()["hours"]


def test_overlapping_intervals_are_not_complete(client):
    """Durations summing to an hour with overlaps must NOT score the hour —
    the union rule detects the overlap and reports the reason."""
    api, _fake, _s = client
    # hour 0: three 15-min intervals starting every 10 minutes → overlaps
    rows = [("2026-11-14T00:00", 1), ("2026-11-14T00:10", 1), ("2026-11-14T00:20", 1)]
    _import_rows(api, rows, 0.25)
    # hour 1: one clean hour
    _import_rows(api, [("2026-11-14T01:00", 2)], 1.0)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z")
    assert acc["actual_hours_in_range"] == 1
    h0 = next(h for h in acc_overlay_hours(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z") if h["valid_at_utc"] == "2026-11-14T00:00:00Z")
    assert h0.get("actual_partial") is True
    assert h0.get("actual_reason") == "overlapping intervals"


def test_gap_even_when_durations_sum_to_one_hour(client):
    """Durations that would sum to an hour, but with a gap: union < 1 h → not
    scoreable, reason 'gap in coverage'."""
    api, _fake, _s = client
    # 00:00–00:15 and 00:45–01:00: sum 30 min here; the point is the hole in
    # the middle — union 30 min, boundaries covered, reason = gap.
    _import_rows(api, [("2026-11-14T00:00", 1), ("2026-11-14T00:45", 1)], 0.25)
    _import_rows(api, [("2026-11-14T01:00", 2)], 1.0)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z")
    assert acc["actual_hours_in_range"] == 1
    h0 = next(h for h in acc_overlay_hours(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z") if h["valid_at_utc"] == "2026-11-14T00:00:00Z")
    assert h0.get("actual_partial") is True
    assert h0.get("actual_reason") == "gap in coverage"


def test_mixed_15_30_60_minute_intervals_form_complete_hour(client):
    api, _fake, _s = client
    _import_rows(api, [("2026-11-14T00:00", 1), ("2026-11-14T00:15", 1)], 0.25)  # 15+15
    _import_rows(api, [("2026-11-14T00:30", 2)], 0.5)  # +30 → hour 0 complete
    _import_rows(api, [("2026-11-14T01:00", 2), ("2026-11-14T01:30", 2)], 0.5)  # 30+30
    _import_rows(api, [("2026-11-14T02:00", 3)], 1.0)  # 60

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T03:00:00Z")
    assert acc["actual_hours_in_range"] == 3, "15/30/60-minute mixed coverage must form complete hours"
    assert acc["actual_hours_partial"] == 0


def test_reading_over_site_limit_blocked_at_import_and_unscoreable_if_legacy(client):
    api, _fake, settings = client
    # default site capacity is 71.5 MW; 80 MWh in one hour → 80 MW → over limit
    r = api.post(
        "/api/actuals/import",
        files={"file": ("over.csv", io.BytesIO(b"ts,mwh\n2026-11-14T00:00:00Z,80\n"), "text/csv")},
        data={"mapping": '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}', "on_conflict": "replace"},
    )
    assert r.status_code == 400, "over-limit readings must be blocked at import (V1.4)"
    assert "physical limit" in r.json()["detail"]["message"]

    # legacy rows inserted before this guard existed must still be excluded
    # from scoring (defense in depth in hourly_actuals), marked with a reason
    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "INSERT INTO actual_points (site_id, interval_start_utc, interval_end_utc, energy_mwh, power_mw, import_id) "
        "VALUES ('krnovo', '2026-11-14T00:00:00Z', '2026-11-14T01:00:00Z', 80, 80, 1)"
    )
    conn.commit()
    conn.close()
    _import_rows(api, [("2026-11-14T01:00", 10)], 1.0)

    acc = _acc(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z")
    assert acc["actual_intervals_over_limit"] == 1
    assert acc["actual_hours_in_range"] == 1  # only the sane hour scores
    h0 = next(h for h in acc_overlay_hours(api, "2026-11-14T00:00:00Z", "2026-11-14T02:00:00Z") if h["valid_at_utc"] == "2026-11-14T00:00:00Z")
    assert h0.get("actual_reason") == "reading exceeds site physical limit"


def test_nan_and_inf_readings_rejected_at_import(client):
    api, _fake, _s = client
    csv_text = "ts,mwh\n2026-11-14T00:00:00Z,nan\n2026-11-14T01:00:00Z,inf\n2026-11-14T02:00:00Z,5\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    r = api.post(
        "/api/actuals/import",
        files={"file": ("nan.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert r.status_code == 400, "non-finite readings must block the import"
    assert api.get("/api/actuals").json()["rows"] == [], "nothing persisted"
