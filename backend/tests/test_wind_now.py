"""V1.6 Addendum A: NOW card — MODELED conditions only.

- /api/forecast marks met_only models explicitly (frontend excludes them
  from Average/totals by flag, never by label string matching).
- /api/assets/{site}/wind-now returns archived model points around "now",
  carries the modeled-not-measured disclaimer, honours the 10-min cache and
  flags stale/missing data honestly.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import make_raw_run

DAY = "2026-11-17"


def _seed_high_wind_now(api, fake, monkeypatch=None):
    """Archive a run whose first hour covers 'now' by freezing the clock."""
    from app import main as main_mod

    if monkeypatch is not None:

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return datetime(2026, 11, 17, 5, 30, tzinfo=timezone.utc)

        monkeypatch.setattr(main_mod, "datetime", _FixedDatetime)

    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    h24 = [f"{DAY}T{h:02d}:00" for h in range(24)]
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(h24, {"80": [12.0] * 24, "100": [12.0] * 24}))
    fake.push_run("ukmo_seamless", f"{DAY}T00:00", make_raw_run(h24, {"10": [8.0] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text
    r2 = api.post("/api/backfill", json={"model_id": "ukmo_seamless", "run_date": DAY, "run_hour": 0, "days": 7})
    assert r2.status_code == 200, r2.text


def test_forecast_entries_carry_met_only_flag(client):
    api, fake, _s = client
    _seed_high_wind_now(api, fake)
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()
    by_id = {m["model_id"]: m for m in fc["models"]}
    assert by_id["gfs_seamless"]["met_only"] is False
    assert by_id["ukmo_seamless"]["met_only"] is True


def test_wind_now_is_modeled_informational(client, monkeypatch):
    api, fake, _s = client
    _seed_high_wind_now(api, fake, monkeypatch)

    r = api.get("/api/assets/krnovo/wind-now")
    assert r.status_code == 200, r.text
    out = r.json()
    # the contract: informational modeled conditions, never measured wind
    assert out["modeled"] is True
    assert "NIJE" in out["disclaimer"] and "stvarni" in out["disclaimer"]

    by_id = {m["model_id"]: m for m in out["models"]}
    g = by_id["gfs_seamless"]
    # now = 05:30 UTC → the 05:00 interval covers it, hub-resolved value
    assert g["valid_at_utc"] == f"{DAY}T05:00:00Z"
    assert g["wind_speed_mps"] is not None
    assert 25 <= g["age_minutes"] <= 35
    assert g["stale"] is False

    u = by_id["ukmo_seamless"]
    assert u["met_only"] is True
    assert u["wind_speed_mps"] == 8.0  # 10 m MET_ONLY level, archived as-is

    # models with no archived run stay explicit, never fabricated
    assert by_id["ecmwf_ifs"]["wind_speed_mps"] is None
    assert by_id["ecmwf_ifs"]["stale"] is True

    # 10-min cache: second read is served from cache (same payload + marker)
    r2 = api.get("/api/assets/krnovo/wind-now").json()
    assert r2["cache"]["cached"] is True
    assert r2["models"] == out["models"]


def test_wind_now_unknown_site_404(client):
    api, _fake, _s = client
    r = api.get("/api/assets/does-not-exist/wind-now")
    assert r.status_code == 404
