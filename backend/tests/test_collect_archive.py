"""Archive behaviour tests (acceptance #3 and #4).

- Two consecutive collections of the SAME forecast cycle do not duplicate rows.
- A NEW issue cycle is retained alongside the old one.
- Points keep null wind as missing (never zero-filled), power respects the cap.
"""

from tests.conftest import make_raw_run

TIMES = [f"2026-11-14T{h:02d}:00" for h in range(6)]  # 00:00..05:00 UTC

WS_GFS = {"80": [6.0, 6.5, 7.0, 7.5, 8.0, None], "100": [6.4, 7.0, 7.4, 8.0, 8.4, 8.8]}
WS_ECMWF = {"100": [6.2, 6.8, 7.2, 7.8, 8.2, 8.6]}
WS_ICON = {"80": [5.8, 6.2, 6.8, 7.2, 7.8, 8.1], "120": [6.8, 7.2, 7.8, 8.2, 8.8, 9.2]}


def _push_cycle(fake_client, run_iso: str) -> None:
    fake_client.push_run("gfs_seamless", run_iso, make_raw_run(TIMES, WS_GFS))
    fake_client.push_run("ecmwf_ifs", run_iso, make_raw_run(TIMES, WS_ECMWF))
    fake_client.push_run("icon_global", run_iso, make_raw_run(TIMES, WS_ICON))
    fake_client.push_run("icon_eu", run_iso, make_raw_run(TIMES, WS_ICON))


def test_same_cycle_twice_does_not_duplicate(client):
    api, fake, _settings = client
    _push_cycle(fake, "2026-11-14T00:00")

    first = api.post("/api/collect", json={"days": 10})
    assert first.status_code == 200, first.text
    ids_first = {r["model_id"]: r["id"] for r in api.get("/api/runs").json()["runs"]}
    assert len(ids_first) == 4

    second = api.post("/api/collect", json={"days": 10})
    assert second.status_code == 200
    runs = api.get("/api/runs").json()["runs"]
    assert len(runs) == 4, "exactly one run per model"
    assert {r["model_id"]: r["id"] for r in runs} == ids_first, "re-collection is idempotent"
    for r in runs:
        assert r["n_points"] == 6
        assert r["run_issued_utc"] == "2026-11-14T00:00:00Z"


def test_same_issue_is_immutable_noop(client):
    """P0 regression: re-collecting the SAME issue must never rewrite archived
    points, raw payload or metadata — even when the (fake) provider now returns
    different values or a longer horizon."""
    import sqlite3

    api, fake, settings = client
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(TIMES, WS_GFS))
    api.post("/api/collect", json={})
    run_id = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"][0]["id"]

    def snapshot():
        conn = sqlite3.connect(settings.db_path)
        try:
            points = conn.execute(
                "SELECT valid_at_utc, wind_speed_mps, wind_direction_deg, modeled_power_mw, "
                "interval_energy_mwh, interval_hours, status FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc",
                (run_id,),
            ).fetchall()
            run = conn.execute(
                "SELECT raw_json, fetched_at_utc, run_issued_utc, issue_source, height_method, height_sources_json "
                "FROM forecast_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        return points, run

    points_a, run_a = snapshot()

    # same issue re-served with DIFFERENT values and an extra hour
    fake.runs["gfs_seamless"] = [
        ("2026-11-14T00:00", make_raw_run(TIMES + ["2026-11-14T06:00"], {"80": [20.0] * 7, "100": [21.0] * 7}))
    ]
    api.post("/api/collect", json={})

    points_b, run_b = snapshot()
    assert points_a == points_b, "archived forecast points must be byte-for-byte immutable"
    assert run_a == run_b, "raw payload, issue/fetch times and provenance must be immutable"
    assert len(api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]) == 1

    # a genuinely NEW issue cycle still creates its own record
    fake.runs["gfs_seamless"].append(("2026-11-14T06:00", make_raw_run(TIMES, WS_GFS)))
    api.post("/api/collect", json={})
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]
    assert len(runs) == 2
    assert sorted(r["run_issued_utc"] for r in runs) == ["2026-11-14T00:00:00Z", "2026-11-14T06:00:00Z"]


def test_new_cycle_retained_alongside_old(client):
    api, fake, _settings = client
    _push_cycle(fake, "2026-11-14T00:00")
    api.post("/api/collect", json={})

    _push_cycle(fake, "2026-11-14T06:00")
    api.post("/api/collect", json={})

    runs = api.get("/api/runs").json()["runs"]
    gfs = [r for r in runs if r["model_id"] == "gfs_seamless"]
    assert len(gfs) == 2, "new issue cycle must be a new row, old kept"
    issues = sorted(r["run_issued_utc"] for r in gfs)
    assert issues == ["2026-11-14T00:00:00Z", "2026-11-14T06:00:00Z"]


def test_height_methods_and_cap(client):
    api, fake, _settings = client
    _push_cycle(fake, "2026-11-14T00:00")
    api.post("/api/collect", json={})

    fc = api.get("/api/forecast", params={"days": 1}).json()
    by_model = {m["model_id"]: m for m in fc["models"]}

    assert by_model["gfs_seamless"]["height_method"] == "log-interp"
    assert by_model["ecmwf_ifs"]["height_method"] == "proxy"
    assert by_model["icon_global"]["height_method"] == "log-interp"
    assert by_model["icon_eu"]["run"]["run_issued_utc"] == "2026-11-14T00:00:00Z"

    gfs_points = by_model["gfs_seamless"]["points"]
    # log-interp of 6.0@80 / 6.4@100 → ~6.11, strictly between the two levels
    assert 6.0 < gfs_points[0]["wind_speed_mps"] < 6.4
    for p in gfs_points:
        assert p["modeled_power_mw"] is None or 0.0 <= p["modeled_power_mw"] <= 71.5

    # the last GFS hour has a null 80 m level → missing, NOT zero-filled
    last = gfs_points[-1]
    assert last["wind_speed_mps"] is None
    assert last["status"] == "missing-height"
    assert last["modeled_power_mw"] is None
    assert last["interval_energy_mwh"] is None


def test_forecast_intersection_is_coverage_aware(client):
    api, fake, _settings = client
    # ecmwf (240h) and icon_eu (120h): give ecmwf 8 hours, icon_eu only 5 hours
    fake.push_run("ecmwf_ifs", "2026-11-14T00:00", make_raw_run(TIMES, WS_ECMWF))
    fake.push_run("icon_eu", "2026-11-14T00:00", make_raw_run(TIMES[:5], {h: WS_ICON[h][:5] for h in WS_ICON}))
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(TIMES, WS_GFS))
    fake.push_run("icon_global", "2026-11-14T00:00", make_raw_run(TIMES, WS_ICON))
    api.post("/api/collect", json={})

    fc = api.get("/api/forecast", params={"days": 1}).json()
    inter = fc["intersection"]
    assert inter["n_common_hours"] == 5
    icon_eu = next(m for m in fc["models"] if m["model_id"] == "icon_eu")
    assert icon_eu["coverage"]["n_hours"] == 5
    gfs = next(m for m in fc["models"] if m["model_id"] == "gfs_seamless")
    assert gfs["coverage"]["n_hours"] == 6


def test_unavailable_archive_falls_back_only_with_explicit_issue_unknown(client):
    api, fake, _settings = client
    # no archived runs at all, but a live response exists for ecmwf only
    fake.live["ecmwf_ifs"] = make_raw_run(TIMES, WS_ECMWF)
    result = api.post("/api/collect", json={}).json()
    statuses = {m["model_id"]: m["status"] for m in result["models"]}
    assert statuses["ecmwf_ifs"] == "ok-issue-unknown"
    assert statuses["gfs_seamless"] == "error"

    runs = api.get("/api/runs").json()["runs"]
    ecm = next(r for r in runs if r["model_id"] == "ecmwf_ifs")
    assert ecm["run_issued_utc"] is None
    assert ecm["issue_source"] == "live-forecast-issue-unknown"

    fc = api.get("/api/forecast", params={"days": 1}).json()
    ecm_entry = next(m for m in fc["models"] if m["model_id"] == "ecmwf_ifs")
    assert any("Issue time unknown" in w for w in ecm_entry["warnings"])
    assert any("not possible" in w for w in ecm_entry["warnings"])


def test_backfill_stores_archived_run(client):
    api, fake, _settings = client
    _push_cycle(fake, "2026-11-13T00:00")
    resp = api.post(
        "/api/backfill",
        json={"model_id": "gfs_seamless", "run_date": "2026-11-13", "run_hour": 0, "days": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["run_issued_utc"] == "2026-11-13T00:00:00Z"

    # idempotent second backfill of the same run: explicit already-archived,
    # stored coverage reported, nothing rewritten
    resp2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-13", "run_hour": 0})
    body2 = resp2.json()
    assert body2["created"] is False
    assert body2["already_archived"] is True
    assert body2["stored_coverage"]["stored_points"] == 6
    assert "already-archived" in body2["note"]
    detail = api.get(f"/api/runs/{body2['run_id']}").json()
    assert len(detail["points"]) == 6  # still exactly the original snapshot

    # missing run → explicit UNAVAILABLE
    resp3 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2020-01-01", "run_hour": 0})
    assert resp3.status_code == 404
    assert "UNAVAILABLE" in resp3.json()["detail"]

    # invalid run hour
    assert api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-13", "run_hour": 99}).status_code == 400


def test_compare_uses_issue_not_fetch_time(client):
    api, fake, _settings = client
    # run ISSUED on the 13th whose valid times fall on the 14th (TIMES fixture)
    _push_cycle(fake, "2026-11-13T00:00")
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-13", "run_hour": 0})
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]
    run_id = runs[0]["id"]

    cmp = api.get("/api/compare", params={"run_id": run_id, "target_date": "2026-11-14"}).json()
    assert cmp["run_issued_utc"] == "2026-11-13T00:00:00Z"
    assert len(cmp["points"]) == 6, "issue of the 13th predicts the 14th — same valid times"
    cmp2 = api.get("/api/compare", params={"run_id": run_id, "target_date": "2026-11-13"}).json()
    assert cmp2["points"] == []  # the run has no valid hours on its issue date


def test_compare_target_date_is_utc_day(client):
    """target_date filters on the UTC day; the UI labels it '(UTC)' — pinned here
    so a local-timezone shift can never creep in silently."""
    api, fake, _settings = client
    times = ["2026-11-13T22:00", "2026-11-13T23:00", "2026-11-14T00:00", "2026-11-14T01:00"]
    fake.push_run("gfs_seamless", "2026-11-12T00:00", make_raw_run(times, {"80": [8.0] * 4, "100": [8.0] * 4}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-12", "run_hour": 0})
    run_id = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"][0]["id"]

    day13 = api.get("/api/compare", params={"run_id": run_id, "target_date": "2026-11-13"}).json()
    assert [p["valid_at_utc"] for p in day13["points"]] == ["2026-11-13T22:00:00Z", "2026-11-13T23:00:00Z"]
    day14 = api.get("/api/compare", params={"run_id": run_id, "target_date": "2026-11-14"}).json()
    assert [p["valid_at_utc"] for p in day14["points"]] == ["2026-11-14T00:00:00Z", "2026-11-14T01:00:00Z"]
