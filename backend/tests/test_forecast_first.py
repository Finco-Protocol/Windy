"""V1.5 Correction A backend tests:

- STRICT archive immutability: same issue re-collected with a LONGER horizon
  (deliberately different values) is a strict no-op — run_id, points, raw
  payload and fetched timestamp unchanged; NEW issues persist independently.
- Daily aggregation in the site's IANA timezone: real 23/24/25 h DST day
  spans, intervals crossing local midnight allocated proportionately,
  partial/complete flags, truncated horizon.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import FakeOMClient, make_raw_run

DAY = "2026-11-13"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00")


def test_same_issue_longer_request_is_immutable_noop(client):
    api, fake, settings = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})

    # 1) archive a 10-hour snapshot of the issue
    times10 = [_iso(datetime.fromisoformat(DAY + "T00:00:00+00:00") + timedelta(hours=i)) for i in range(10)]
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times10, {"80": [8.0] * 10, "100": [8.0] * 10}))
    r1 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 10})
    assert r1.status_code == 200
    run_id = r1.json()["run_id"]
    original_ts = r1.json()["points"]

    import sqlite3

    def snapshot():
        conn = sqlite3.connect(settings.db_path)
        try:
            run = conn.execute(
                "SELECT raw_json, fetched_at_utc, forecast_days FROM forecast_runs WHERE id=?", (run_id,)
            ).fetchone()
            pts = conn.execute(
                "SELECT valid_at_utc, wind_speed_mps, modeled_power_mw, interval_energy_mwh "
                "FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return run, pts

    before = snapshot()

    # 2) re-request the SAME issue at 16 days with deliberately DIFFERENT values
    times16 = [_iso(datetime.fromisoformat(DAY + "T00:00:00+00:00") + timedelta(hours=i)) for i in range(16)]
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times16, {"80": [30.0] * 16, "100": [31.0] * 16}))
    r2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["run_id"] == run_id
    assert r2.json()["points"] == 10, "stored point count reported unchanged"

    after = snapshot()
    assert after[0][0] == before[0][0], "raw payload unchanged"
    assert after[0][1] == before[0][1], "fetched timestamp unchanged"
    assert after[0][2] == 10, "forecast_days unchanged"
    assert after[1] == before[1], "archived points byte-for-byte unchanged"

    # 3) a NEW issue at 16 days persists independently with full horizon
    day2 = "2026-11-14"
    times_new = [_iso(datetime.fromisoformat(day2 + "T00:00:00+00:00") + timedelta(hours=i)) for i in range(16)]
    fake.push_run("gfs_seamless", f"{day2}T00:00", make_raw_run(times_new, {"80": [8.0] * 16, "100": [8.0] * 16}))
    r3 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day2, "run_hour": 0, "days": 16})
    assert r3.status_code == 200
    assert r3.json()["created"] is True
    assert r3.json()["points"] == 16

    runs = api.get("/api/runs", params={"site_id": "krnovo", "model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs) == 2
    after_all = snapshot()
    assert after_all[1] == before[1], "historical accuracy rows unchanged after the new issue"


def test_daily_real_local_days_and_midnight_crossing(client):
    """Europe/Podgorica: real local-day spans; 2-h intervals crossing local
    midnight are allocated proportionately (1 h to each CET day)."""
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # 2-h intervals every 2 h from 00:00Z; the LAST interval 22:00Z→00:00Z
    # crosses local (CET) midnight and must be split 1 h + 1 h
    start = datetime.fromisoformat(DAY + "T00:00:00+00:00")
    times = [(start + timedelta(hours=i * 2)).strftime("%Y-%m-%dT%H:00") for i in range(13)]
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times, {"80": [8.0] * 13, "100": [8.0] * 13}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16, "site_id": "krnovo"})
    assert r.status_code == 200, r.text

    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()
    gfs = next(m for m in fc["models"] if m["model_id"] == "gfs_seamless")
    daily = {d["date"]: d for d in gfs["daily_local"]}
    # CET day Nov 13 = [23:00Z Nov 12, 23:00Z Nov 13): 12 full intervals (00–24h
    # minus the midnight-crossing half) → 22 h; CET Nov 14: crossing half + none
    assert daily["2026-11-13"]["hours_expected"] == 24
    assert daily["2026-11-13"]["hours_counted"] == pytest.approx(22.0, abs=1e-6)
    assert daily["2026-11-13"]["partial"] is True
    assert daily["2026-11-14"]["hours_counted"] == pytest.approx(3.0, abs=1e-6)
    assert daily["2026-11-14"]["partial"] is True
    # energy allocated proportionately (equal per-hour values → 22/24 and 2/24)
    total = sum(d["mwh"] for d in gfs["daily_local"])
    assert total == pytest.approx(daily["2026-11-13"]["mwh"] + daily["2026-11-14"]["mwh"], abs=1e-6)


def test_dst_local_day_spans_23_and_25(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # spring: issue 2026-03-28T22:00Z, 24 hourly intervals cover the 23 h local
    # day 2026-03-29 (22:00Z Mar 28 → 22:00Z Mar 29)
    start = datetime.fromisoformat("2026-03-28T22:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(24)]
    fake.push_run("gfs_seamless", "2026-03-28T22:00", make_raw_run(times, {"80": [8.0] * 24, "100": [8.0] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-03-28", "run_hour": 22, "days": 16, "site_id": "krnovo"})
    assert r.status_code == 200
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()
    gfs = next(m for m in fc["models"] if m["model_id"] == "gfs_seamless")
    daily = {d["date"]: d for d in gfs["daily_local"]}
    assert daily["2026-03-29"]["hours_expected"] == pytest.approx(23.0, abs=1e-3)
    assert daily["2026-03-29"]["partial"] is False, "24 valid intervals fully cover the 23 h local day"

    # fall: local day 2026-10-25 = 25 h starting 22:00Z Oct 24 → 25 intervals
    start_f = datetime.fromisoformat("2026-10-24T22:00:00+00:00")
    times_f = [(start_f + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(25)]
    fake.push_run("gfs_seamless", "2026-10-24T22:00", make_raw_run(times_f, {"80": [8.0] * 25, "100": [8.0] * 25}))
    r2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-10-24", "run_hour": 22, "days": 16, "site_id": "krnovo"})
    assert r2.status_code == 200
    fc2 = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()
    gfs2 = next(m for m in fc2["models"] if m["model_id"] == "gfs_seamless")
    daily2 = {d["date"]: d for d in gfs2["daily_local"]}
    assert daily2["2026-10-25"]["hours_expected"] == pytest.approx(25.0, abs=1e-3)
    assert daily2["2026-10-25"]["partial"] is False
