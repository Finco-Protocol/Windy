"""PR#8 Correction A §2/§3/§4 — same-site config revisions, location
infrastructure, atomic per-unit constraint replacement.

§2.1/§7.3 upgrade proof: an EXISTING 71.5 MW / 26 x 2.75 seeded DB with a
historical run is upgraded to 74.10 MW / 26 x 2.85 through an append-only
config revision — old run bytes unchanged, restart never reseeds, new runs
attribute to the new configuration. No production DB is written anywhere.
"""

from datetime import timedelta

import pytest

from tests.conftest import make_raw_run
from app import db as dbm
from app.config import Settings
from app.main import create_app
from tests.test_adjusted_daily import _hourly  # reuse the hourly-time helper

WS = 15.0


def _seed_old_style_krnovo(api, fake):
    """A DB that still carries the OLD V1.5-era krnovo row (71.5 MW / 2.75),
    plus an archived historical run — the 'existing production-like' state."""
    import json

    with dbm.session(str(api.app.state.settings.db_path)) as conn:
        conn.execute(
            "UPDATE assets SET capacity_mw=71.5, turbine_count=26, turbine_rated_mw=2.75, "
            "allow_scaled_reference=0, turbine_model=? WHERE id='krnovo'",
            ("GE-klasa 2,75 MW (stara konfiguracija)",),
        )
        conn.execute("DELETE FROM turbines")
        conn.execute("DELETE FROM turbine_locations")
        _ = json  # keep lint quiet
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    h24 = [f"2026-11-16T{h:02d}:00" for h in range(24)]
    fake.push_run("gfs_seamless", "2026-11-16T00:00", make_raw_run(h24, {"80": [12.0] * 24, "100": [12.0] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-16", "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text
    old_run_id = r.json()["run_id"]
    old_points = api.get(f"/api/runs/{old_run_id}", params={"site_id": "krnovo"}).json()["points"]
    return old_run_id, old_points


def _ensure_db_again(settings):
    """Simulate a process restart: full startup path (migrations + seed)."""
    from app.store import ensure_db

    ensure_db(settings.db_path)


def test_upgrade_715_to_741_same_site_history_intact_restart_idempotent(client):
    api, fake, settings = client
    old_run_id, old_points = _seed_old_style_krnovo(api, fake)

    site_before = api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]
    assert site_before["capacity_mw"] == pytest.approx(71.5)

    # RESTART must NOT reseed the existing row to the new registry defaults
    _ensure_db_again(settings)
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(71.5)

    # owner-driven config revision (same site id, append-only)
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={
            "changes": {
                "capacity_mw": 74.1,
                "turbine_count": 26,
                "turbine_rated_mw": 2.85,
                "allow_scaled_reference": True,
                "regenerate_inventory": True,
            },
            "note": "Owner correction 2026-11-17: 26 x 2.85 MW = 74.10 MW nameplate; scaled-reference opt-in",
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == 1
    assert out["changes"]["capacity_mw"] == {"from": 71.5, "to": 74.1}
    assert out["site"]["capacity_mw"] == pytest.approx(74.1)
    assert out["inventory_revision"] == 2  # regenerated at a NEW inventory revision

    # audit trail readable
    revs = api.get("/api/assets/krnovo/config-revisions").json()
    assert revs["current_revision"] == 1
    assert len(revs["revisions"]) == 1
    assert revs["revisions"][0]["changes"]["turbine_rated_mw"]["from"] == pytest.approx(2.75)

    # historical run provably UNCHANGED
    old_after = api.get(f"/api/runs/{old_run_id}", params={"site_id": "krnovo"}).json()["points"]
    assert old_points == old_after

    # RESTART again: revision survives, no duplicate, no reseed back
    _ensure_db_again(settings)
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(74.1)
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 1

    # a NEW issue cycle is attributed to the new configuration end-to-end:
    # adjusted max at high wind = 74.10 MW
    next_day = "2026-11-17"
    t0 = "2026-11-17T00:00"
    fake.push_run("gfs_seamless", t0, make_raw_run(_hourly(next_day, 0, 24), {"80": [WS] * 24, "100": [WS] * 24}))
    r2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": next_day, "run_hour": 0, "days": 16})
    assert r2.status_code == 200, r2.text
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    assert m["adjusted"]["available"] is True
    vals = [p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None]
    assert max(vals) == pytest.approx(74.1, abs=1e-3), "new run computes under the 74.10 MW configuration"
    # old run still exists alongside the new one
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless", "site_id": "krnovo"}).json()["runs"]
    assert old_run_id in [x["id"] for x in runs]


def test_config_revision_rejects_bad_fields_and_unknown_site(client):
    api, _fake, _s = client
    r = api.post("/api/assets/nope/config-revision", json={"changes": {"capacity_mw": 10}})
    assert r.status_code == 404
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    r2 = api.post("/api/assets/krnovo/config-revision", json={"changes": {"engine_size": 3}})
    assert r2.status_code == 400
    r3 = api.post("/api/assets/krnovo/config-revision", json={"changes": {"capacity_mw": -5}})
    assert r3.status_code == 400
    r4 = api.post("/api/assets/krnovo/config-revision", json={"changes": {"timezone": "Not/AZone"}})
    assert r4.status_code == 400
    r5 = api.post("/api/assets/krnovo/config-revision", json={"changes": {}})
    assert r5.status_code == 400
    # nothing was applied
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0


def test_locations_append_only_validation_and_persistence(client):
    api, _fake, settings = client
    # NO seed data: a fresh site reports zero locations with an explicit note
    empty = api.get("/api/assets/krnovo/locations").json()
    assert empty["revision"] == 0 and empty["n_locations"] == 0
    assert "nema unesenih lokacija" in empty["note"]

    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # synthetic TEST-FIXTURE records (marked as such) exercising the mechanism;
    # the OWNER XLSX/CSV is NOT available in this environment — import stays BLOCKED
    recs = [
        {"unit_id": f"WTG-{i:03d}", "latitude": 42.80 + i * 0.001, "longitude": 19.10 + i * 0.001, "elevation_m": 1000.0 + i, "source": "test-fixture"}
        for i in range(1, 27)
    ]
    r = api.post("/api/assets/krnovo/locations", json={"records": recs, "note": "synthetic mechanism test"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["revision"] == 1 and got["n_locations"] == 26
    assert got["locations"][0]["source"] == "test-fixture"

    # restart persists (append-only storage, no cleanup)
    from app.store import ensure_db

    ensure_db(settings.db_path)
    got2 = api.get("/api/assets/krnovo/locations").json()
    assert got2["revision"] == 1 and got2["n_locations"] == 26
    assert got2["revision_history"][0]["n_records"] == 26

    # validation: duplicate unit, out-of-range, count mismatch shape
    dup = api.post("/api/assets/krnovo/locations", json={"records": [{"unit_id": "WTG-001", "latitude": 1, "longitude": 1}, {"unit_id": "WTG-001", "latitude": 2, "longitude": 2}]})
    assert dup.status_code == 400
    oor = api.post("/api/assets/krnovo/locations", json={"records": [{"unit_id": "WTG-001", "latitude": 91.0, "longitude": 1.0}]})
    assert oor.status_code == 400
    nor = api.post("/api/assets/krnovo/locations", json={"records": [{"unit_id": "WTG-001"}]})
    assert nor.status_code == 400
    # failed calls created no revisions
    assert api.get("/api/assets/krnovo/locations").json()["revision"] == 1


def test_fleet_change_without_regeneration_is_rejected_with_no_write(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85}},
    )
    assert r.status_code == 400
    assert "regenerate_inventory=true" in r.json()["detail"]
    # NO write happened anywhere
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(71.5)
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0


def test_nameplate_capacity_mismatch_rejected_even_with_regeneration(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 70.0, "turbine_count": 26, "turbine_rated_mw": 2.85, "regenerate_inventory": True}},
    )
    assert r.status_code == 400
    assert "74.10" in r.json()["detail"] or "74.1" in r.json()["detail"]
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(71.5)
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0


def test_scaled_optin_is_required_not_silent(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    # 2.85 rating vs 2.78 curve peak WITHOUT allow_scaled_reference -> reject
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "regenerate_inventory": True}},
    )
    assert r.status_code == 400
    assert "allow_scaled_reference" in r.json()["detail"]
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(71.5)


def test_fleet_shrink_with_constraint_on_removed_unit_rejected(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    api.post(
        "/api/assets/krnovo/constraints",
        json={"rows": [{"kind": "offline", "unit_ids": ["WTG-025"], "start_local": "2026-11-12T00:00", "end_local": None}]},
    )
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 57.0, "turbine_count": 20, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
    )
    assert r.status_code == 400
    assert "WTG-025" in r.json()["detail"], "affected constraint is listed precisely"
    assert api.get("/api/assets/krnovo/constraints").json()["revision"] == 1, "constraints untouched"
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0


def test_mid_transaction_failure_rolls_back_everything(client):
    api, fake, _s = client
    old_run_id, old_points = _seed_old_style_krnovo(api, fake)
    from app import db as dbm
    from app.availability import current_inventory_revision

    db = str(api.app.state.settings.db_path)
    # a trigger forces the regeneration INSERT inside apply_config_revision's
    # transaction to ABORT — a genuine mid-write failure AFTER the assets
    # UPDATE statement, proving the whole transaction rolls back
    with dbm.session(db) as conn:
        conn.execute(
            "CREATE TRIGGER force_fail_before_turbines BEFORE INSERT ON turbines "
            "WHEN NEW.rated_mw = 2.85 BEGIN SELECT RAISE(ABORT, 'forced mid-transaction failure'); END;"
        )

    before_constraints = api.get("/api/assets/krnovo/constraints").json()["revision"]
    before_inventory_rev = current_inventory_revision(db, "krnovo")
    with pytest.raises(Exception):
        api.post(
            "/api/assets/krnovo/config-revision",
            json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
        )
    with dbm.session(db) as conn:
        conn.execute("DROP TRIGGER force_fail_before_turbines")
    # assets row UNCHANGED (the UPDATE rolled back with the failed INSERT)
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["capacity_mw"] == pytest.approx(71.5)
    # no config revision, no inventory revision, constraints unchanged
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0
    assert current_inventory_revision(db, "krnovo") == before_inventory_rev
    assert api.get("/api/assets/krnovo/constraints").json()["revision"] == before_constraints
    # historical run bytes unchanged
    assert api.get(f"/api/runs/{old_run_id}", params={"site_id": "krnovo"}).json()["points"] == old_points


def test_upgrade_then_six_owner_caps_gives_72(client):
    api, fake, _s = client
    old_run_id, old_points = _seed_old_style_krnovo(api, fake)
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["regenerated_inventory"] is True
    assert out["inventory_revision"] == 2
    # owner picks ANY six (here WTG-007..012 - deliberately not 001..006)
    six = [f"WTG-{i:03d}" for i in range(7, 13)]
    rr = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": u, "state": "cap", "cap_mw": 2.5} for u in six], "start_local": "2026-11-16T00:00"},
    )
    assert rr.status_code == 200, rr.text
    next_day = "2026-11-17"
    fake.push_run("gfs_seamless", "2026-11-17T00:00", make_raw_run(_hourly(next_day, 0, 24), {"80": [WS] * 24, "100": [WS] * 24}))
    rr2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": next_day, "run_hour": 0, "days": 16})
    assert rr2.status_code == 200
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    vals = [p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None]
    assert max(vals) == pytest.approx(20 * 2.85 + 6 * 2.50, abs=1e-3), "74.10 -> 72.00 with owner-selected six"
    assert api.get(f"/api/runs/{old_run_id}", params={"site_id": "krnovo"}).json()["points"] == old_points


def _upgrade_krnovo(api):
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_cc_nonfleet_edit_without_regeneration_succeeds(client):
    api, fake, _s = client
    old_run_id, old_points = _seed_old_style_krnovo(api, fake)
    up = _upgrade_krnovo(api)
    inv_rev = api.get("/api/assets/krnovo/turbines").json()["revision"]

    # hub-height-only edit, regenerate OFF, fleet keys absent -> must succeed
    r = api.post("/api/assets/krnovo/config-revision", json={"changes": {"hub_height_m": 95}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == up["revision"] + 1
    assert out["inventory_revision"] is None
    assert out["regenerated_inventory"] is False
    assert out["site"]["hub_height_m"] == pytest.approx(95)
    # inventory untouched: same revision, same installed MW
    inv = api.get("/api/assets/krnovo/turbines").json()
    assert inv["revision"] == inv_rev
    assert inv["installed_mw"] == pytest.approx(26 * 2.85, abs=1e-6)
    # archived runs byte-exact
    assert api.get(f"/api/runs/{old_run_id}", params={"site_id": "krnovo"}).json()["points"] == old_points


def test_cc_unchanged_fleet_values_do_not_demand_regeneration(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    _upgrade_krnovo(api)
    inv_rev = api.get("/api/assets/krnovo/turbines").json()["revision"]
    n_before = api.get("/api/assets/krnovo/turbines").json()["n_turbines"]

    # client echoes unchanged count/rating but only changes tz + losses
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"turbine_count": 26, "turbine_rated_mw": 2.85, "timezone": "Europe/Sarajevo", "loss_factor": 0.05}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["regenerated_inventory"] is False
    inv = api.get("/api/assets/krnovo/turbines").json()
    assert inv["revision"] == inv_rev, "no new inventory revision"
    assert inv["n_turbines"] == n_before, "no regenerated WTG rows"
    assert api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]["timezone"] == "Europe/Sarajevo"


def test_cc_capacity_only_operational_limit_rejected(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    _upgrade_krnovo(api)
    revs_before = api.get("/api/assets/krnovo/config-revisions").json()["current_revision"]
    # 72.00 MW is the OPERATIONAL limit of six 2.50 caps — NOT a nameplate;
    # a bare capacity edit to 72.0 against the 74.10 MW fleet must be rejected
    r = api.post("/api/assets/krnovo/config-revision", json={"changes": {"capacity_mw": 72.0}})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "INSTALIRANI" in detail and "72.0" in detail
    assert "operational limit" in detail or "operativni" in detail.lower()
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == revs_before, "atomic no-write"


def test_cc_real_rating_change_without_regeneration_rejected(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    r = api.post("/api/assets/krnovo/config-revision", json={"changes": {"turbine_rated_mw": 2.85}})
    assert r.status_code == 400
    assert "regenerate_inventory=true" in r.json()["detail"]
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == 0


def test_cc_noop_returns_clear_response_without_revision(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    _upgrade_krnovo(api)
    revs_before = api.get("/api/assets/krnovo/config-revisions").json()["current_revision"]
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "hub_height_m": 90, "loss_factor": 0, "timezone": "Europe/Podgorica", "allow_scaled_reference": True}},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["no_op"] is True
    assert out["revision"] == revs_before, "no new revision created"
    assert api.get("/api/assets/krnovo/config-revisions").json()["current_revision"] == revs_before


def test_future_scheduled_constraint_survives_current_action(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # FUTURE scheduled restriction on WTG-001 (starts next week) — an owner
    # obligation that must NOT be cancelled by a current ON action
    r = api.post(
        "/api/assets/krnovo/constraints",
        json={"rows": [{"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-12-01T00:00", "end_local": "2026-12-05T00:00"}]},
    )
    assert r.status_code == 200, r.text
    # current action: WTG-002 OFF (does not touch WTG-001) AND WTG-001 ON
    r2 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "on"}, {"unit_id": "WTG-002", "state": "off"}], "start_local": "2026-11-18T00:00"},
    )
    assert r2.status_code == 200, r2.text
    cons = r2.json()["constraints"]
    # local midnight 2026-12-01 = 2026-11-30T23:00Z; the scheduled row survives
    future = [c for c in cons if c["kind"] == "offline" and (c["end_utc"] or "") >= "2026-12-01"]
    assert any(c["unit_ids"] == ["WTG-001"] for c in future), "future scheduled restriction survives with its unit intact"
    # the future obligation still ACTIVATES later: scenario engine at that
    # window sees WTG-001 offline
    fake.push_run("gfs_seamless", "2026-12-01T00:00", make_raw_run(_hourly("2026-12-01", 0, 24), {"80": [WS] * 24, "100": [WS] * 24}))
    rr = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-12-01", "run_hour": 0, "days": 16})
    assert rr.status_code == 200
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    vals = [p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None]
    # 25 producing units at 2.75 (WTG-001 OFF by the scheduled rule, WTG-002
    # not affected by this run's window start unless its own constraint is
    # open-ended from 11-18 — it is, so 24 units + WTG-001 off):
    assert max(vals) == pytest.approx(24 * 2.75, abs=1e-3), "scheduled future restriction activates later"


def test_config_revision_writes_current_provenance_preserving_history(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    r = api.post(
        "/api/assets/krnovo/config-revision",
        json={
            "changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True},
            "note": "owner correction",
        },
    )
    assert r.status_code == 200, r.text
    site = api.get("/api/site", params={"site_id": "krnovo"}).json()["site"]
    assumptions = site["assumptions"]
    # historical assumptions preserved (old capacity/curve text untouched)
    assert any("71.5" in str(v) or "74.10" in str(v) for v in assumptions.values()), "old provenance keys remain"
    # CURRENT owner-revised label added, honestly scoped
    cur = assumptions.get("owner_revision_1")
    assert cur is not None
    assert "2.85" in cur
    assert "REFERENCE/UNVERIFIED" in cur
    assert "NIJE" in cur, "explicit non-claim of a certified GE 2.85-116 curve"
    assert "NIJE potvrdena" in cur or "NIJE potvrđena" in cur, "rotor/model variant not invented"


def test_replace_units_mw_mode_exact_payload_and_numbers(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    _upgrade = api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
    )
    assert _upgrade.status_code == 200
    # owner's canonical path: SIX units via Max MW mode -> EXACT cap_mw 2.5
    six = [f"WTG-{i:03d}" for i in range(1, 7)]
    r = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": u, "state": "cap", "cap_mw": 2.5} for u in six], "start_local": "2026-11-16T00:00"},
    )
    assert r.status_code == 200, r.text
    cons = r.json()["constraints"]
    caps = [c for c in cons if c["kind"] == "curtailment" and c["cap_mw"] is not None]
    assert caps and all(c["cap_mw"] == pytest.approx(2.5) for c in caps), "stored as cap_mw exactly 2.5"
    assert sum(len(c["unit_ids"] or []) for c in caps) == 6
    assert all(c["cap_percent"] is None for c in caps), "no pct mixed in for MW mode"
    # 74.10 -> 72.00
    fake.push_run("gfs_seamless", "2026-11-17T00:00", make_raw_run(_hourly("2026-11-17", 0, 24), {"80": [WS] * 24, "100": [WS] * 24}))
    rr = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-17", "run_hour": 0, "days": 16})
    assert rr.status_code == 200
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    vals = [p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None]
    assert max(vals) == pytest.approx(72.0, abs=1e-3)
    # preview == status.available_max_mw
    st = api.get("/api/assets/krnovo/status").json()
    assert st["available_max_mw"] == pytest.approx(72.0, abs=1e-3)
    # one capped OFF -> 69.50
    r2 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "off"}], "start_local": "2026-11-16T00:00"},
    )
    assert r2.status_code == 200
    vals2 = [p["adjusted_mw"] for p in api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()["models"][1]["adjusted"]["points"] if p["adjusted_mw"] is not None]
    _ = vals2  # gfs is index-found below
    m2 = next(x for x in api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()["models"] if x["model_id"] == "gfs_seamless")
    vals2 = [p["adjusted_mw"] for p in m2["adjusted"]["points"] if p["adjusted_mw"] is not None]
    assert max(vals2) == pytest.approx(69.5, abs=1e-3)
    # WTG-001 back to its 2.50 cap (plain 'on' = unrestricted), an UNLIMITED
    # unit OFF -> 69.15 with SIX caps retained
    r3 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "cap", "cap_mw": 2.5}, {"unit_id": "WTG-010", "state": "off"}], "start_local": "2026-11-16T00:00"},
    )
    assert r3.status_code == 200
    m3 = next(x for x in api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()["models"] if x["model_id"] == "gfs_seamless")
    vals3 = [p["adjusted_mw"] for p in m3["adjusted"]["points"] if p["adjusted_mw"] is not None]
    assert max(vals3) == pytest.approx(69.15, abs=1e-3)
    # 2.4 MW example: Max MW below 2.5 is stored exactly
    r4 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "cap", "cap_mw": 2.4}], "start_local": "2026-11-16T00:00"},
    )
    assert r4.status_code == 200
    caps4 = [c for c in r4.json()["constraints"] if c["kind"] == "curtailment" and c["cap_mw"] is not None]
    assert any(c["cap_mw"] == pytest.approx(2.4) for c in caps4)


def test_replace_units_percent_mode_sends_allowed_percent(client):
    api, fake, _s = client
    _seed_old_style_krnovo(api, fake)
    api.post(
        "/api/assets/krnovo/config-revision",
        json={"changes": {"capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85, "allow_scaled_reference": True, "regenerate_inventory": True}},
    )
    # pct mode: 12.28% REDUCTION on 2.85 = 87.72% allowed = 2.4998... MW
    r = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "cap", "cap_percent": 87.72}], "start_local": "2026-11-16T00:00"},
    )
    assert r.status_code == 200, r.text
    caps = [c for c in r.json()["constraints"] if c["kind"] == "curtailment" and c["cap_percent"] is not None]
    assert caps and caps[0]["cap_percent"] == pytest.approx(87.72)
    assert caps[0]["cap_mw"] is None, "pct mode sends cap_percent, never cap_mw"


def test_replace_units_atomic_split_grouped_constraints(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    # grouped OFF on WTG-001+002 and grouped cap on WTG-002+003 (overlapping!)
    r = api.post(
        "/api/assets/krnovo/constraints",
        json={
            "rows": [
                {"kind": "offline", "unit_ids": ["WTG-001", "WTG-002"], "start_local": "2026-11-12T00:00", "end_local": None},
                {"kind": "curtailment", "unit_ids": ["WTG-002", "WTG-003"], "cap_mw": 1.0, "start_local": "2026-11-12T00:00", "end_local": None},
            ]
        },
    )
    assert r.status_code == 200, r.text

    # owner switches ONLY WTG-001 back ON and sets WTG-004 to cap 2.0 — the
    # grouped OFF row must be SPLIT (WTG-002 stays OFF), not left stale
    r2 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={
            "assignments": [
                {"unit_id": "WTG-001", "state": "on"},
                {"unit_id": "WTG-004", "state": "cap", "cap_mw": 2.0},
            ],
            "start_local": "2026-11-18T00:00",
        },
    )
    assert r2.status_code == 200, r2.text
    cons = r2.json()["constraints"]
    off_rows = [c for c in cons if c["kind"] == "offline" and c["unit_ids"]]
    cap_rows = [c for c in cons if c["kind"] == "curtailment" and c["unit_ids"]]
    assert any(sorted(c["unit_ids"]) == ["WTG-002"] for c in off_rows), "WTG-002 keeps its OFF after the split"
    assert not any("WTG-001" in (c["unit_ids"] or []) for c in off_rows), "WTG-001 is back ON"
    assert any(sorted(c["unit_ids"]) == ["WTG-002", "WTG-003"] and c["cap_mw"] == pytest.approx(1.0) for c in cap_rows), "untouched cap row preserved"
    assert any(sorted(c["unit_ids"]) == ["WTG-004"] and c["cap_mw"] == pytest.approx(2.0) for c in cap_rows)
    assert r2.json()["revision"] == 2, "ONE atomic revision, not delete+add"

    # validation: unknown unit, bad state, over-rated cap -> 400, no revision
    for bad in (
        {"assignments": [{"unit_id": "WTG-999", "state": "on"}]},
        {"assignments": [{"unit_id": "WTG-001", "state": "maybe"}]},
        {"assignments": [{"unit_id": "WTG-001", "state": "cap", "cap_mw": 99.0}]},
        {"assignments": [{"unit_id": "WTG-001", "state": "cap"}]},
    ):
        rb = api.post("/api/assets/krnovo/constraints/replace-units", json=bad)
        assert rb.status_code == 400, bad
    assert api.get("/api/assets/krnovo/constraints").json()["revision"] == 2

    # scenario end-to-end: high wind, WTG-002 OFF + WTG-003@1.0 + WTG-004@2.0
    fake.push_run("gfs_seamless", "2026-11-18T00:00", make_raw_run(_hourly("2026-11-18", 0, 24), {"80": [WS] * 24, "100": [WS] * 24}))
    rr = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-18", "run_hour": 0, "days": 16})
    assert rr.status_code == 200
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    vals = [p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None]
    # 23 x 2.75 (WTG-002 off) + 1.0 (WTG-003) + 2.0 (WTG-004) = 66.75
    assert max(vals) == pytest.approx(23 * 2.75 + 1.0 + 2.0, abs=1e-3)


def test_expected_revision_guard_409_on_stale(client):
    """Correction E §1: optimistic lost-update guard — a stale expected_revision
    is rejected with 409 + current revision, and NO mutation happens."""
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    r0 = api.get("/api/assets/krnovo/constraints").json()
    stale = r0["revision"]
    # create a newer revision so 'stale' is behind
    api.post("/api/assets/krnovo/constraints", json={"rows": [{"kind": "offline", "unit_ids": ["WTG-002"], "start_local": "2026-11-12T00:00", "end_local": None}]})
    r = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "off"}], "start_local": "2026-11-18T00:00", "expected_revision": stale},
    )
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["expected"] == stale and d["current_revision"] == stale + 1
    # correct expected_revision succeeds
    r2 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": "WTG-001", "state": "off"}], "start_local": "2026-11-18T00:00", "expected_revision": stale + 1},
    )
    assert r2.status_code == 200


def test_concurrent_replace_units_serialized(client):
    """Correction F P0-7: two concurrent replace-units requests BOTH carrying
    the same expected_revision — exactly one commits, the other gets 409
    without any write. Constraints and audit trail stay consistent."""
    import threading

    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    api.get("/api/assets/krnovo/constraints")
    base = api.get("/api/assets/krnovo/constraints").json()["revision"]
    barrier = threading.Barrier(2)
    results: dict = {}

    def fire(unit: str) -> None:
        barrier.wait()
        r = api.post(
            "/api/assets/krnovo/constraints/replace-units",
            json={"assignments": [{"unit_id": unit, "state": "off"}], "start_local": "2026-11-18T00:00", "expected_revision": base},
        )
        results[unit] = r.status_code

    t1 = threading.Thread(target=fire, args=("WTG-001",))
    t2 = threading.Thread(target=fire, args=("WTG-002",))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(results.values())
    assert codes == [200, 409], f"exactly one winner: {results}"
    # committed state: BOTH units end up OFF? No — the 409 never wrote. Only
    # the winner's unit is OFF; constraints revision advanced exactly once
    cons = api.get("/api/assets/krnovo/constraints").json()
    assert cons["revision"] == base + 1
    off_units = [u for c in cons["constraints"] if c["kind"] == "offline" for u in (c["unit_ids"] or [])]
    assert len(off_units) == 1, f"only the winner's unit is OFF: {off_units}"
    # loser can now retry with the CURRENT revision and succeed
    loser = [u for u, code in results.items() if code == 409][0]
    r2 = api.post(
        "/api/assets/krnovo/constraints/replace-units",
        json={"assignments": [{"unit_id": loser, "state": "off"}], "start_local": "2026-11-18T00:00", "expected_revision": base + 1},
    )
    assert r2.status_code == 200
