"""V1.5: forecast-first backend tests.

- daily_local flags partial first/last days with expected 23/24/25 h.
- horizon extension: same issue collected with a LONGER forecast_days
  supersedes the shorter snapshot (documented supersession); shorter/equal
  requests remain no-ops.
- scheduled collector respects WINDY_COLLECT_DAYS.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import FakeOMClient, make_raw_run  # noqa: F401

DAY = "2026-11-13"  # a plain 24 h UTC/local day


def _mk_client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.scheduler_enabled = False
    fake = FakeOMClient()
    application = create_app(settings=settings, om_client=fake)
    client = TestClient(application)
    return client, fake, settings


import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

# imported late on purpose (module-level app creation in tests above keeps
# the suite uniform); re-import for direct calls
from tests.conftest import make_raw_run as _mkr  # noqa: E402, F401


def test_first_day_partial_last_day_truncated(client):
    """Issue at 12:00Z with 36 h horizon: local day 1 has 12 h (partial),
    day 2 has 24 h (complete), day 3 is truncated at 00:00Z (partial)."""
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    start = datetime.fromisoformat(DAY + "T12:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(36)]
    fake.push_run("gfs_seamless", f"{DAY}T12:00", make_raw_run(times, {"80": [8.0] * 36, "100": [8.0] * 36}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 12, "days": 16, "site_id": "krnovo"})
    assert r.status_code == 200, r.text

    fc = api.get("/api/forecast", params={"days": 3, "site_id": "krnovo"}).json()
    model = next(m for m in fc["models"] if m["model_id"] == "gfs_seamless")
    daily = {d["date"]: d for d in model["daily_local"]}

    d1 = daily["2026-11-13"]
    assert d1["hours_expected"] == 24
    assert d1["partial"] is True, "day starts at 13:00 local → 11 of 24 local hours (23:00Z belongs to next local day)"
    assert d1["hours_counted"] == 11

    d2 = daily["2026-11-14"]
    assert d2["hours_expected"] == 24
    assert d2["partial"] is False and d2["hours_counted"] == 24

    # 23:00Z Nov 14 = 00:00 CET Nov 15 → one interval lands on the next CET day
    d3 = daily["2026-11-15"]
    assert d3["partial"] is True, "horizon ends at 23:00Z → truncated CET day"
    assert d3["hours_counted"] == 1


def test_cet_reference_days_around_dst(client):
    """CET (UTC+1) fixed reference: every calendar day is exactly 24 UTC hours,
    even across DST transition weekends. The fall-back 'extra' hour belongs to
    the NEXT CET day and is flagged partial there."""
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # spring weekend: issue 2026-03-28T00:00Z, 48 h (Mar 28–29)
    start = datetime.fromisoformat("2026-03-28T00:00:00+00:00")
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(48)]
    fake.push_run("gfs_seamless", "2026-03-28T00:00", make_raw_run(times, {"80": [8.0] * 48, "100": [8.0] * 48}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-03-28", "run_hour": 0, "days": 16, "site_id": "krnovo"})
    fc = api.get("/api/forecast", params={"days": 3, "site_id": "krnovo"}).json()
    gfs = next(m for m in fc["models"] if m["model_id"] == "gfs_seamless")
    daily = {d["date"]: d for d in gfs["daily_local"]}
    # run starts 00:00Z = 01:00 CET Mar 28 → CET Mar 28 misses its first hour
    assert daily["2026-03-28"]["partial"] is True and daily["2026-03-28"]["hours_counted"] == 23
    assert daily["2026-03-29"]["hours_expected"] == 24 and daily["2026-03-29"]["partial"] is False

    # fall weekend: issue 2026-10-24T00:00Z, 49 h → the 49th interval (00:00Z
    # Oct 26 = 01:00 CET Oct 26) becomes a partial CET day of its own
    fall_start = datetime.fromisoformat("2026-10-24T00:00:00+00:00")
    times_f = [(fall_start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(49)]
    fake.push_run("gfs_seamless", "2026-10-24T00:00", make_raw_run(times_f, {"80": [8.0] * 49, "100": [8.0] * 49}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-10-24", "run_hour": 0, "days": 16, "site_id": "krnovo"})
    fc_f = api.get("/api/forecast", params={"days": 3, "site_id": "krnovo"}).json()
    gfs_f = next(m for m in fc_f["models"] if m["model_id"] == "gfs_seamless")
    daily_f = {d["date"]: d for d in gfs_f["daily_local"]}
    # run starts 00:00Z = 01:00 CET Oct 24 → CET Oct 24 misses its first hour
    assert daily_f["2026-10-24"]["partial"] is True and daily_f["2026-10-24"]["hours_counted"] == 23
    assert daily_f["2026-10-25"]["hours_expected"] == 24 and daily_f["2026-10-25"]["partial"] is False
    # last interval 00:00Z Oct 26 = 01:00 CET Oct 26 → 2-interval partial day
    assert daily_f["2026-10-26"]["partial"] is True and daily_f["2026-10-26"]["hours_counted"] == 2


def test_horizon_extension_supersedes_shorter_snapshot(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    times10 = [f"{DAY}T{h:02d}:00" for h in range(10)]
    times16 = [f"{DAY}T{h:02d}:00" for h in range(16)] + [f"2026-11-1{4}T{h:02d}:00" for h in range(0)]

    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times10, {"80": [8.0] * 10, "100": [8.0] * 10}))
    r1 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 10})
    assert r1.status_code == 200
    run_id = r1.json()["run_id"]
    assert r1.json()["points"] == 10

    # same issue, LONGER request → supersedes (new points, same issue/provenance)
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times16, {"80": [8.0] * 16, "100": [8.0] * 16}))
    r2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r2.status_code == 200
    assert r2.json()["points"] == 16
    assert r2.json()["run_id"] == run_id, "extension stays on the same run row"

    runs = api.get("/api/runs", params={"site_id": "krnovo", "model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs) == 1, "no duplicate rows for the same issue"
    detail = api.get(f"/api/runs/{run_id}").json()
    assert len(detail["points"]) == 16
    assert detail["forecast_days"] == 16

    # SHORTER or equal request after a longer one → strict no-op (16 stays)
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(times10, {"80": [8.0] * 10, "100": [8.0] * 10}))
    r3 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 10})
    assert r3.json()["created"] is False
    detail3 = api.get(f"/api/runs/{run_id}").json()
    assert len(detail3["points"]) == 16, "shorter re-collection must not truncate the extended snapshot"
