"""Archive health/coverage report tests."""

from tests.conftest import make_raw_run

TIMES = [f"2026-11-14T{h:02d}:00" for h in range(6)]
WS = {"80": [6.0] * 6, "100": [6.4] * 6}  # GFS levels
WS_ICON = {"80": [5.8] * 6, "120": [6.8] * 6}  # ICON levels


def test_archive_health_counts_and_missing_cycles(client):
    api, fake, _s = client
    # ecmwf: collect stores only the newest cycle; backfill adds the second
    fake.push_run("ecmwf_ifs", "2026-11-14T00:00", make_raw_run(TIMES, {"100": [6.0] * 6}))
    fake.push_run("ecmwf_ifs", "2026-11-14T06:00", make_raw_run(TIMES, {"100": [6.0] * 6}))
    api.post("/api/collect", json={})
    api.post("/api/backfill", json={"model_id": "ecmwf_ifs", "run_date": "2026-11-14", "run_hour": 0})
    # gfs: archive empty → live fallback → issue-unknown run
    fake.live["gfs_seamless"] = make_raw_run(TIMES, WS)
    api.post("/api/collect", json={})

    health = api.get("/api/archive/health").json()["models"]
    by_id = {m["model_id"]: m for m in health}

    ecm = by_id["ecmwf_ifs"]
    assert ecm["known_issue_cycles"] == 2
    assert ecm["issue_unknown_runs"] == 0
    assert ecm["first_issue_utc"] == "2026-11-14T00:00:00Z"
    assert ecm["last_issue_utc"] == "2026-11-14T06:00:00Z"
    assert ecm["total_points"] == 12
    assert ecm["n_missing_cycles"] == 0  # 00 and 06 are adjacent

    gfs = by_id["gfs_seamless"]
    assert gfs["known_issue_cycles"] == 0
    assert gfs["issue_unknown_runs"] == 1
    assert gfs["first_issue_utc"] is None
    assert gfs["n_missing_cycles"] is None
    assert "not scoreable" in gfs["note"]


def test_archive_health_reports_gap_cycles(client):
    api, fake, _s = client
    # two cycles 12 h apart → only 06 sits in the gap; collect stores 12:00,
    # backfill adds 00:00
    fake.push_run("icon_eu", "2026-11-14T00:00", make_raw_run(TIMES, WS_ICON))
    fake.push_run("icon_eu", "2026-11-14T12:00", make_raw_run(TIMES, WS_ICON))
    api.post("/api/collect", json={})
    api.post("/api/backfill", json={"model_id": "icon_eu", "run_date": "2026-11-14", "run_hour": 0})

    ie = {m["model_id"]: m for m in api.get("/api/archive/health").json()["models"]}["icon_eu"]
    assert ie["n_missing_cycles"] == 1
    assert ie["missing_cycles"] == ["2026-11-14T06:00:00Z"]
