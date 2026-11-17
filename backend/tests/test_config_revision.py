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
