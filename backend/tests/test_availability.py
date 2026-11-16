"""V1.3 numerical and behavioural acceptance tests (owner's six cases plus
windows/DST, scale, isolation, immutability/revision and missing-data rules)."""

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.availability import (
    AvailabilityError,
    local_to_utc,
    site_status,
)
from tests.conftest import make_raw_run

DAY = "2026-11-14"
HOURS = [f"{DAY}T{h:02d}:00" for h in range(6)]  # 00..05 UTC, 8 m/s wind


def _create_site(api, **over):
    body = {
        "id": "farm-a",
        "label": "Farm A",
        "latitude": 45.0,
        "longitude": 16.0,
        "capacity_mw": 71.5,
        "turbine_count": 26,
        "turbine_rated_mw": 2.75,
        "hub_height_m": 90,
        "loss_factor": 0.0,
        "timezone": "Europe/Podgorica",
        "power_curve_id": "ge103-2750-oedb",
    }
    body.update(over)
    r = api.post("/api/assets", json=body)
    assert r.status_code == 200, r.text
    return body


def _push_run(api, fake, ws=8.0, n=6, site_id="farm-a", day=DAY):
    times = [f"{day}T{h:02d}:00" for h in range(n)]
    fake.push_run("gfs_seamless", f"{day}T00:00", make_raw_run(times, {"80": [ws] * n, "100": [ws] * n}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": day, "run_hour": 0, "days": 2, "site_id": site_id})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _gen(api, site_id, count=26, rating=2.75):
    r = api.post(f"/api/assets/{site_id}/turbines/generate", json={"count": count, "rating_mw": rating})
    assert r.status_code == 200, r.text
    return r.json()


def _constrain(api, site_id, rows):
    r = api.post(f"/api/assets/{site_id}/constraints", json={"rows": rows})
    assert r.status_code == 200, r.text
    return r.json()


def _scenario(api, site_id, run_id):
    r = api.get(f"/api/assets/{site_id}/scenario", params={"run_id": run_id})
    assert r.status_code == 200, r.text
    return r.json()


# ---- case 1: all full, no cap → 71.50 MW at rated wind -------------------------


def test_case1_all_full_reproduces_7150_at_rated(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)  # above rated → curve at max
    sc = _scenario(api, "farm-a", run_id)

    rated_point = next(p for p in sc["points"] if p["status"] == "ok")
    # per-unit contribution clamps at nameplate 2.75 (curve max 2.78 ≥ rated):
    # pre_export = 26 × 2.75, farm cap clips nothing further → exactly 71.50 MW
    assert rated_point["pre_export_mw"] == pytest.approx(71.5, abs=1e-3)
    assert rated_point["adjusted_mw"] == pytest.approx(71.5, abs=1e-3)
    assert sc["summary"]["curve_mode"] == "direct"
    assert sc["installed_mw"] == 71.5


def test_case1b_all_full_matches_v1_baseline(client):
    api, fake, _s = client
    _create_site(api, loss_factor=0.05)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=8.0)
    sc = _scenario(api, "farm-a", run_id)
    for p in sc["points"]:
        assert p["adjusted_mw"] == pytest.approx(p["base_mw"], abs=1e-3), (
            "all-turbines-full + no export cap must reproduce the v1 baseline "
            "(single loss application, same farm cap)"
        )
        assert p["lost_to_unit_mwh"] == pytest.approx(0.0, abs=1e-6)
        assert p["lost_to_export_mwh"] == pytest.approx(0.0, abs=1e-6)


# ---- case 2: 2 offline + 4 OTHER at 50% → 60.50 MW ------------------------------


def test_case2_two_offline_four_half_cap(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)

    _constrain(
        api,
        "farm-a",
        [
            {"kind": "offline", "unit_ids": ["WTG-001", "WTG-002"], "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "curtailment", "unit_ids": ["WTG-003", "WTG-004", "WTG-005", "WTG-006"], "cap_percent": 50, "start_local": "2026-11-13T00:00", "end_local": None},
        ],
    )
    sc = _scenario(api, "farm-a", run_id)
    rated = next(p for p in sc["points"] if p["status"] == "ok")
    # 20 × min(2.78, 2.75) + 4 × min(2.78, 1.375) = 55 + 5.5 — NOT 71.5×(24/26)×0.5
    assert rated["pre_export_mw"] == pytest.approx(20 * 2.75 + 4 * 1.375, abs=1e-3)
    assert rated["adjusted_mw"] == pytest.approx(60.5, abs=1e-3)


def test_case2b_low_wind_cap_does_not_halve_output(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    # 3 m/s → GE curve ≈ 0.017 MW per unit, far below the 1.375 cap
    run_id = _push_run(api, fake, ws=3.0)
    _constrain(
        api,
        "farm-a",
        [{"kind": "curtailment", "unit_ids": ["WTG-001"], "cap_percent": 50, "start_local": "2026-11-13T00:00", "end_local": None}],
    )
    sc = _scenario(api, "farm-a", run_id)
    p0 = next(p for p in sc["points"] if p["status"] == "ok")
    from app.power import load_curve

    expected_unit = load_curve().power_mw_per_turbine(3.0)  # 0.017 MW — not halved
    assert p0["adjusted_mw"] == pytest.approx(25 * expected_unit + expected_unit, abs=1e-2), \
        "a 1.375 MW cap must not reduce a 0.017 MW unconstrained output"


# ---- case 3: 45 MW farm export cap, losses without double counting --------------


def test_case3_farm_export_cap_and_loss_split(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)
    _constrain(
        api,
        "farm-a",
        [
            {"kind": "offline", "unit_ids": ["WTG-001", "WTG-002"], "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "curtailment", "unit_ids": ["WTG-003", "WTG-004", "WTG-005", "WTG-006"], "cap_percent": 50, "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-13T00:00", "end_local": None},
        ],
    )
    sc = _scenario(api, "farm-a", run_id)
    p = next(p for p in sc["points"] if p["status"] == "ok")
    assert p["adjusted_mw"] == pytest.approx(45.0, abs=1e-3)
    # rated-clamped base: 20×2.75 + 4×2.75(clamped) = 71.5 = stored baseline
    assert p["pre_export_mw"] == pytest.approx(60.5, abs=1e-3)
    assert p["lost_to_unit_mwh"] == pytest.approx(71.5 - 60.5, abs=1e-2)
    assert p["lost_to_export_mwh"] == pytest.approx(60.5 - 45.0, abs=1e-3)
    # no double counting: base − total loss == adjusted (1 h interval)
    total_lost = p["lost_to_unit_mwh"] + p["lost_to_export_mwh"]
    assert p["base_mw"] - total_lost == pytest.approx(p["adjusted_mw"], abs=1e-2)


# ---- case 4: time windows, boundaries, DST --------------------------------------


def test_case4_time_window_boundaries_and_dst(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    # hours 00..15 UTC on the spring-forward day
    run_id = _push_run(api, fake, ws=15.0, day="2026-03-29", n=16)
    # WTG-001 offline 07:00–16:00 LOCAL (= 06:00–15:00 UTC winter→summer day)
    _constrain(
        api,
        "farm-a",
        [{"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-03-29T07:00", "end_local": "2026-03-29T16:00"}],
    )
    cons = api.get("/api/assets/farm-a/constraints").json()["constraints"]
    # local 07:00–16:00 on the spring-forward day = 05:00–14:00 UTC (CEST, UTC+2 after 03:00 local)
    assert cons[0]["start_utc"] == "2026-03-29T05:00:00Z"
    assert cons[0]["end_utc"] == "2026-03-29T14:00:00Z"

    sc = _scenario(api, "farm-a", run_id)
    def adj(h):
        return next(p for p in sc["points"] if p["valid_at_utc"] == f"2026-03-29T{h:02d}:00:00Z")["adjusted_mw"]

    # 04:00 UTC (06:00 local, before window) → 26; 05:00 UTC (07:00 local, inside) → 25
    assert adj(4) == pytest.approx(26 * 2.75, abs=1e-3)
    assert adj(5) == pytest.approx(25 * 2.75, abs=1e-3)
    assert adj(13) == pytest.approx(25 * 2.75, abs=1e-3)  # inside (end 14:00Z exclusive)
    assert adj(14) == pytest.approx(26 * 2.75, abs=1e-3)  # after end → full again
    # fall-back day: ambiguous 02:00 local resolves to first occurrence, no crash
    with pytest.raises(AvailabilityError):
        local_to_utc("2026-03-29T02:30", "Europe/Podgorica")  # nonexistent → explicit error
    utc, warn = local_to_utc("2026-10-25T02:30", "Europe/Podgorica")
    assert utc == "2026-10-25T00:30:00Z" and warn is not None


def test_case4b_overlapping_events_most_restrictive_wins(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)
    # two overlapping curtailments on WTG-001: 1.8 MW and 1.0 MW → 1.0 wins
    _constrain(
        api,
        "farm-a",
        [
            {"kind": "curtailment", "unit_ids": ["WTG-001"], "cap_mw": 1.8, "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "curtailment", "unit_ids": ["WTG-001"], "cap_mw": 1.0, "start_local": "2026-11-13T00:00", "end_local": None},
        ],
    )
    sc = _scenario(api, "farm-a", run_id)
    p = next(p for p in sc["points"] if p["status"] == "ok")
    # 25 units at rated-clamped 2.75 + WTG-001 at its 1.0 MW cap
    assert p["pre_export_mw"] == pytest.approx(25 * 2.75 + 1.0, abs=1e-3)


def test_case4c_offline_dominates_curtailment(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)
    _constrain(
        api,
        "farm-a",
        [
            {"kind": "curtailment", "unit_ids": ["WTG-001"], "cap_mw": 1.5, "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-11-13T00:00", "end_local": None},
        ],
    )
    status = site_status(api.app.state.settings.db_path if hasattr(api, "app") else None, "farm-a") if False else None
    sc = _scenario(api, "farm-a", run_id)
    p = next(p for p in sc["points"] if p["status"] == "ok")
    assert p["pre_export_mw"] == pytest.approx(25 * 2.75, abs=1e-3), "offline → 0 regardless of an active cap"


# ---- case 5: mixed ratings, validation -------------------------------------------


def test_case5_mixed_ratings_and_validation(client):
    api, fake, _s = client
    # mixed fleet: 36 common 2.0 MW units, 10 of them upgraded to 3.0 MW later;
    # declared nameplate 71.5 reconciled explicitly
    _create_site(api, capacity_mw=71.5, reconcile_nameplate=True, turbine_rated_mw=2.0, turbine_count=36)
    api.post("/api/assets/farm-a/turbines/generate", json={"count": 36, "rating_mw": 2.0})
    # per-unit overrides: 10 units upgraded to 3.0 → sum = 10×3.0 + 26×2.0 = 82.0
    edits = [{"unit_id": f"WTG-{i:03d}", "rated_mw": 3.0} for i in range(1, 11)]
    inv = api.post("/api/assets/farm-a/turbines/bulk-ratings", json={"edits": edits}).json()
    assert inv["installed_mw"] == pytest.approx(10 * 3.0 + 26 * 2.0, abs=1e-6)

    # 3.0 MW units on the 2.75-max GE curve need explicit scaled-reference;
    # without opt-in → explicit missing-curve, never a silent mismatch
    run_id = _push_run(api, fake, ws=15.0)
    sc = _scenario(api, "farm-a", run_id)
    assert all(p["status"] == "missing-curve" for p in sc["points"]), \
        "a 2.75 MW-max curve must not be silently used for 3.0 MW units without opt-in"

    # validation rejects: fractional count, negative cap, over-nameplate cap, NaN
    r = api.post("/api/assets", json={
        "id": "bad-count", "label": "BC", "latitude": 45, "longitude": 16, "capacity_mw": 7.75,
        "turbine_count": 2.5, "turbine_rated_mw": 2.75, "hub_height_m": 90,
        "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    })
    assert r.status_code == 400
    r = api.post("/api/assets/farm-a/constraints", json={"rows": [
        {"kind": "curtailment", "unit_ids": ["WTG-011"], "cap_mw": -1, "start_local": "2026-11-13T00:00"}
    ]})
    assert r.status_code == 400
    r = api.post("/api/assets/farm-a/constraints", json={"rows": [
        {"kind": "curtailment", "unit_ids": ["WTG-011"], "cap_mw": 3.5, "start_local": "2026-11-13T00:00"}
    ]})
    assert r.status_code == 400, "cap above per-unit nameplate must be rejected"
    r = api.post("/api/assets/farm-a/constraints", json={"rows": [
        {"kind": "curtailment", "unit_ids": ["WTG-011"], "cap_mw": "NaN", "start_local": "2026-11-13T00:00"}
    ]})
    assert r.status_code == 400

    # scaled-reference opt-in: smaller site where the GE curve is weaker than
    # the units — normalised shape × per-unit rating, labelled
    r = api.post("/api/assets", json={
        "id": "scaled-site", "label": "Scaled", "latitude": 45, "longitude": 16,
        "capacity_mw": 30.0, "turbine_count": 10, "turbine_rated_mw": 3.0, "hub_height_m": 90,
        "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb", "allow_scaled_reference": True,
    })
    assert r.status_code == 200, r.text
    api.post("/api/assets/scaled-site/turbines/generate", json={"count": 10, "rating_mw": 3.0})
    run_s = _push_run(api, fake, ws=15.0, site_id="scaled-site")
    sc_s = _scenario(api, "scaled-site", run_s)
    assert sc_s["summary"]["curve_mode"] == "scaled-reference"
    ps = next(p for p in sc_s["points"] if p["status"] == "ok")
    assert ps["adjusted_mw"] == pytest.approx(30.0, abs=1e-2)  # 10 × (2.78/2.78×3.0) clipped at farm cap


# ---- case 6: scale (250 turbines, one bulk request) + isolation ------------------


def test_case6_scale_250_turbines_one_bulk_request(client):
    api, fake, _s = client
    _create_site(api, id="big", label="Big", capacity_mw=687.5, turbine_count=250, turbine_rated_mw=2.75)
    r = api.post("/api/assets/big/turbines/generate", json={"count": 250, "rating_mw": 2.75})
    assert r.status_code == 200
    assert r.json()["n_turbines"] == 250

    # ONE request constrains all 250 units (no per-unit HTTP calls)
    unit_ids = [f"WTG-{i:03d}" for i in range(1, 251)]
    r = api.post(
        "/api/assets/big/constraints",
        json={"rows": [{"kind": "curtailment", "unit_ids": unit_ids, "cap_percent": 50, "start_local": "2026-11-13T00:00", "end_local": None}]},
    )
    assert r.status_code == 200, r.text
    cons = r.json()
    assert len(cons["constraints"]) == 1 and len(cons["constraints"][0]["unit_ids"]) == 250

    # the run's scenario handles 250 units in one pass
    run_id = _push_run(api, fake, ws=15.0, site_id="big")
    sc = _scenario(api, "big", run_id)
    p = next(p for p in sc["points"] if p["status"] == "ok")
    assert p["pre_export_mw"] == pytest.approx(250 * 1.375, abs=1e-3), "50% cap of 2.75 MW = 1.375 MW per unit"


def test_case6b_site_isolation(client):
    api, fake, _s = client
    _create_site(api)  # farm-a with cap below
    _gen(api, "farm-a", 26, 2.75)
    body_b = {
        "id": "farm-b", "label": "Farm B", "latitude": 44.0, "longitude": 17.0,
        "capacity_mw": 71.5, "turbine_count": 26, "turbine_rated_mw": 2.75,
        "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb",
    }
    assert api.post("/api/assets", json=body_b).status_code == 200
    _gen(api, "farm-b", 26, 2.75)

    run_a = _push_run(api, fake, ws=15.0, site_id="farm-a")
    run_b = _push_run(api, fake, ws=15.0, site_id="farm-b")
    _constrain(api, "farm-a", [{"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-13T00:00", "end_local": None}])

    a = _scenario(api, "farm-a", run_a)
    b = _scenario(api, "farm-b", run_b)
    assert a["points"][0]["adjusted_mw"] == pytest.approx(45.0, abs=1e-3)
    assert b["points"][0]["adjusted_mw"] == pytest.approx(71.5, abs=1e-3), "site A's export cap must never touch site B"

    # cross-site scenario access is a 404
    r = api.get("/api/assets/farm-a/scenario", params={"run_id": run_b})
    assert r.status_code == 404


# ---- immutability & revisions ------------------------------------------------------


def test_revisions_immutable_after_publish(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    run_id = _push_run(api, fake, ws=15.0)
    _constrain(api, "farm-a", [{"kind": "export_cap", "cap_mw": 50, "start_local": "2026-11-13T00:00", "end_local": None}])

    pub_a = api.post("/api/assets/farm-a/scenario/publish", json={"run_id": run_id}).json()
    summary_a = json.dumps(pub_a["summary"], sort_keys=True)

    def snapshot():
        conn = sqlite3.connect(_s.db_path)
        try:
            run = conn.execute("SELECT raw_json, run_issued_utc FROM forecast_runs WHERE id=?", (run_id,)).fetchone()
            pts = conn.execute("SELECT valid_at_utc, modeled_power_mw, interval_energy_mwh FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc", (run_id,)).fetchall()
            rev = conn.execute("SELECT summary_json FROM scenario_revisions WHERE id=?", (pub_a["scenario_revision_id"],)).fetchone()[0]
        finally:
            conn.close()
        return run, pts, rev

    run_a, pts_a, rev_a = snapshot()

    # revise the schedule: DELETE the 50 MW cap in a new revision, then add 60
    cons = api.get("/api/assets/farm-a/constraints").json()
    old_id = cons["constraints"][0]["id"]
    api.post("/api/assets/farm-a/constraints/delete", json={"ids": [old_id]})
    _constrain(api, "farm-a", [{"kind": "export_cap", "cap_mw": 60, "start_local": "2026-11-13T00:00", "end_local": None}])
    pub_b = api.post("/api/assets/farm-a/scenario/publish", json={"run_id": run_id}).json()
    assert pub_b["scenario_revision_id"] != pub_a["scenario_revision_id"]
    assert json.dumps(pub_b["summary"], sort_keys=True) != summary_a

    run_b, pts_b, rev_b = snapshot()
    assert run_a == run_b, "stored source run must be bit-for-bit unchanged"
    assert pts_a == pts_b, "original base MWh must be unchanged"
    assert rev_a == rev_b, "published scenario revision A must be unchanged"

    # revision A (50 MW cap) and revision B (60 MW cap) differ as expected —
    # window values are ENERGY over the 6 archived hours (50 MW × 6 h = 300 MWh)
    summary_b = json.loads(json.dumps(pub_b["summary"], sort_keys=True))
    summary_a_parsed = json.loads(summary_a)
    assert summary_a_parsed["windows"]["24h"]["adjusted_mwh"] == pytest.approx(6 * 50.0, abs=1e-2)
    assert summary_b["windows"]["24h"]["adjusted_mwh"] == pytest.approx(6 * 60.0, abs=1e-2)

    # republishing identical inputs is idempotent
    pub_a2 = api.post("/api/assets/farm-a/scenario/publish", json={"run_id": run_id, "constraints_revision": 1}).json()
    assert pub_a2["scenario_revision_id"] == pub_a["scenario_revision_id"]


# ---- missing data -------------------------------------------------------------------


def test_missing_wind_and_missing_curve_stay_null(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    # a run whose wind has nulls at the first hour
    times = HOURS
    raw = make_raw_run(times, {"80": [None] + [8.0] * 5, "100": [None] + [8.0] * 5})
    fake.push_run("gfs_seamless", f"{DAY}T00:00", raw)
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 2, "site_id": "farm-a"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    _constrain(api, "farm-a", [{"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-13T00:00", "end_local": None}])
    sc = _scenario(api, "farm-a", run_id)
    first = sc["points"][0]
    assert first["status"] == "missing-wind"
    assert first["adjusted_mw"] is None
    assert first["lost_to_unit_mwh"] is None, "losses stay null, never zero, for missing wind"

    # shorter model than 10 days: points simply end — no padding
    assert len(sc["points"]) == 6


def test_status_endpoint_counts(client):
    api, fake, _s = client
    _create_site(api)
    _gen(api, "farm-a", 26, 2.75)
    _constrain(
        api,
        "farm-a",
        [
            {"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-11-13T00:00", "end_local": None},
            {"kind": "curtailment", "unit_ids": ["WTG-002"], "cap_percent": 50, "start_local": "2026-11-13T00:00", "end_local": None},
        ],
    )
    st = api.get("/api/assets/farm-a/status").json()
    assert st["offline_count"] == 1 and st["curtailed_count"] == 1
    assert st["available_max_mw"] == pytest.approx(24 * 2.75 + 1.375, abs=1e-3)
    assert st["installed_mw"] == 71.5 and st["farm_export_cap_mw"] is None
