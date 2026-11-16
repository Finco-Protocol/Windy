"""P0-1: lead-bin selection correctness.

Multiple issue times predicting the SAME actual hour must populate every bin
whose lead actually exists; the overall total must count the hour exactly once
(latest issue), never a sum over bins. Overlay with a lead-bin filter must
show the issue from that bin, not the globally latest one.
"""

import io

import pytest

from tests.conftest import make_raw_run

TARGET_HOUR = "2026-11-14T12:00"  # the actual hour everything predicts


def _push_run(fake, issue, n_hours=6, ws=8.0):
    """A run issued at `issue` covering TARGET_HOUR with n_hours of points."""
    base = TARGET_HOUR[:13]  # hour 12
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(TARGET_HOUR)
    times = [(t0 + timedelta(hours=i - 2)).strftime("%Y-%m-%dT%H:00") for i in range(n_hours)]
    fake.push_run("gfs_seamless", issue, make_raw_run(times, {"80": [ws] * n_hours, "100": [ws] * n_hours}))


def _backfill(api, issue):
    day, hour = issue.split("T")
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day, "run_hour": int(hour[:2]), "days": 16})
    assert r.status_code == 200, r.text


def _import_one_actual_hour(api):
    csv_text = f"ts,mwh\n{TARGET_HOUR}:00Z,10"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    r = api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert r.status_code == 200 and r.json()["imported"] is True, r.text


def test_same_hour_populates_multiple_bins_and_overall_dedupes(client):
    api, fake, _s = client
    # three genuine 18Z issues at 18 h, 42 h and 90 h before the target hour
    _push_run(fake, "2026-11-13T18:00")  # lead 18 h → day-ahead bin
    _push_run(fake, "2026-11-12T18:00")  # lead 42 h → 2-day bin
    _push_run(fake, "2026-11-10T18:00")  # lead 90 h → 5-day bin
    for issue in ("2026-11-13T18:00", "2026-11-12T18:00", "2026-11-10T18:00"):
        _backfill(api, issue)
    _import_one_actual_hour(api)

    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    gfs = acc["models"]["gfs_seamless"]
    bins = gfs["bins"]
    assert set(bins) == {"day-ahead (0-24h)", "2-day ahead (24-48h)", "5-day ahead (72-120h)"}, \
        "the same target hour must appear in every bin whose lead actually exists"
    for label in bins:
        assert bins[label]["n_pairs"] == 1

    # overall counts the hour ONCE (latest issue = day-ahead), not 3 times
    total = gfs["total"]
    assert total["n_pairs"] == 1
    assert total["distinct_hours"] == 1
    assert acc["selection_policy"].startswith("bins: latest archived issue per")


def test_recent_issue_cannot_displace_older_bin_prediction(client):
    api, fake, _s = client
    # only a 5-day-bin issue exists; then a day-ahead issue arrives later
    _push_run(fake, "2026-11-10T18:00")  # lead 90 h → 5-day bin
    _backfill(api, "2026-11-10T18:00")
    _push_run(fake, "2026-11-14T00:00")  # lead 12 h → day-ahead
    _backfill(api, "2026-11-14T00:00")
    _import_one_actual_hour(api)

    acc = api.get(
        "/api/accuracy",
        params={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    bins = acc["models"]["gfs_seamless"]["bins"]
    assert set(bins) == {"day-ahead (0-24h)", "5-day ahead (72-120h)"}, \
        "the fresh day-ahead issue must not erase the genuine 5-day bin entry"


def test_overlay_lead_filter_uses_bin_issue(client):
    api, fake, _s = client
    _push_run(fake, "2026-11-12T18:00", ws=6.0)  # lead 42 h → 2-day bin, lower wind
    _push_run(fake, "2026-11-13T18:00", ws=9.0)  # lead 18 h → day-ahead, higher wind
    _backfill(api, "2026-11-12T18:00")
    _backfill(api, "2026-11-13T18:00")
    _import_one_actual_hour(api)

    from app.power import farm_power_mw, load_curve

    pf6 = farm_power_mw(6.0, load_curve(), 26, 0.0, 71.5)
    pf9 = farm_power_mw(9.0, load_curve(), 26, 0.0, 71.5)

    ov_2day = api.get(
        "/api/accuracy/overlay",
        params={
            "from_utc": "2026-11-14T00:00:00Z",
            "to_utc": "2026-11-15T00:00:00Z",
            "lead_bin": "2-day ahead (24-48h)",
        },
    ).json()
    got = [h for h in ov_2day["hours"] if h["valid_at_utc"] == "2026-11-14T12:00:00Z"][0]
    assert got["models"]["gfs_seamless"] == pytest.approx(pf6, abs=1e-3), "2-day view must use the 2-day issue"

    ov_da = api.get(
        "/api/accuracy/overlay",
        params={
            "from_utc": "2026-11-14T00:00:00Z",
            "to_utc": "2026-11-15T00:00:00Z",
            "lead_bin": "day-ahead (0-24h)",
        },
    ).json()
    got = [h for h in ov_da["hours"] if h["valid_at_utc"] == "2026-11-14T12:00:00Z"][0]
    assert got["models"]["gfs_seamless"] == pytest.approx(pf9, abs=1e-3), "day-ahead view must use the day-ahead issue"


def test_calibration_policy_is_latest_issue_per_hour(client):
    api, fake, _s = client
    # two issues covering the same hours; calibration must train on the latest
    # per hour (declared policy), never on both (double counting)
    _push_run(fake, "2026-11-12T18:00", ws=8.0)
    _push_run(fake, "2026-11-13T18:00", ws=8.0)
    _backfill(api, "2026-11-12T18:00")
    _backfill(api, "2026-11-13T18:00")
    _import_one_actual_hour(api)

    res = api.post(
        "/api/calibration/train",
        json={"from_utc": "2026-11-14T00:00:00Z", "to_utc": "2026-11-15T00:00:00Z"},
    ).json()
    entry = res["models"]["gfs_seamless"]
    assert entry["status"] == "not-calibrated-insufficient"  # 1 hour << thresholds
    assert entry["n_train_hours"] + entry["n_val_hours"] == 1, "one measured hour counted exactly once"
    cal = {c["model_id"]: c for c in api.get("/api/calibration").json()["models"]}
    assert "latest known issue per" in cal["gfs_seamless"]["note"] or "selection policy" in cal["gfs_seamless"]["note"]
