"""V1.6: /api/forecast?with_availability=1 — adjusted forecast integrated into
the main forecast endpoint, reusing the tested scenario math.

Owner acceptance numbers (26 × 2.75 MW, loss_factor 0, high wind 15 m/s):
1. all full, no cap → 71.5 MW
2. 2 offline → 66.0 MW
3. 2 offline + 4 DIFFERENT at 50% rated → 60.5 MW
4. + farm export cap 45 → 45.0 MW
5. low wind + 50% cap → min(unconstrained, 1.375), not ×0.5
"""

from datetime import datetime, timedelta

import pytest

from tests.conftest import FakeOMClient, make_raw_run

from app.power import load_curve

DAY = "2026-11-13"
H24 = [f"{DAY}T{h:02d}:00" for h in range(24)]
WS_HIGH = 15.0


def _setup(api, fake):
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(H24, {"80": [WS_HIGH] * 24, "100": [WS_HIGH] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _constrain(api, rows, site="krnovo"):
    r = api.post(f"/api/assets/{site}/constraints", json={"rows": rows})
    assert r.status_code == 200, r.text
    return r.json()


def _adj(api):
    r = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"})
    assert r.status_code == 200, r.text
    return r.json()


def _max_adj(fc):
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    return max(p["adjusted_mw"] for p in m["adjusted"]["points"] if p["adjusted_mw"] is not None)


def test_adjusted_numbers_715_660_605_45(client):
    api, fake, _s = client
    _setup(api, fake)

    # 1) all full, no constraints → 71.5 MW (rated clamp at high wind)
    fc = _adj(api)
    assert _max_adj(fc) == pytest.approx(71.5, abs=1e-3)
    assert fc["operational"]["installed_mw"] == 71.5
    assert fc["operational"]["offline_count"] == 0

    # 2) 2 offline → 66.0 MW
    _constrain(api, [{"kind": "offline", "unit_ids": ["WTG-001", "WTG-002"], "start_local": "2026-11-12T00:00", "end_local": None}])
    assert _max_adj(_adj(api)) == pytest.approx(66.0, abs=1e-3)
    st = api.get("/api/assets/krnovo/status").json()
    assert st["offline_count"] == 2 and st["available_max_mw"] == pytest.approx(66.0, abs=1e-3)

    # 3) + 4 DIFFERENT units at 50% of rated → 60.5 MW (24×2.75 + 4×1.375)
    _constrain(api, [{"kind": "curtailment", "unit_ids": ["WTG-003", "WTG-004", "WTG-005", "WTG-006"], "cap_percent": 50, "start_local": "2026-11-12T00:00", "end_local": None}])
    assert _max_adj(_adj(api)) == pytest.approx(60.5, abs=1e-3)

    # 4) + farm export cap 45 → 45.0 MW
    _constrain(api, [{"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-12T00:00", "end_local": None}])
    assert _max_adj(_adj(api)) == pytest.approx(45.0, abs=1e-3)


def test_low_wind_50pct_cap_is_minimum_not_multiplier(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    raw_unit = load_curve().power_mw_per_turbine(3.0)  # ~0.017 MW at 3 m/s
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(H24, {"80": [3.0] * 24, "100": [3.0] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    run_id = r.json()["run_id"]
    _constrain(api, [{"kind": "curtailment", "unit_ids": ["WTG-001"], "cap_percent": 50, "start_local": "2026-11-12T00:00", "end_local": None}])
    sc = api.get("/api/assets/krnovo/scenario", params={"run_id": run_id}).json()
    first = sc["points"][0]
    assert first["adjusted_mw"] == pytest.approx(26 * raw_unit, rel=1e-6), \
        "1.375 MW cap must not halve a ~0.017 MW unconstrained output"


def test_baseline_and_wind_unchanged_by_constraints(client):
    api, fake, _s = client
    run_id = _setup_with_run(api, fake)
    before = api.get(f"/api/runs/{run_id}").json()["points"]
    before_wind = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()["models"][0]["points"]
    _constrain(api, [{"kind": "export_cap", "cap_mw": 45, "start_local": "2026-11-12T00:00", "end_local": None}])
    after = api.get(f"/api/runs/{run_id}").json()["points"]
    after_wind = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"}).json()["models"][0]["points"]
    assert before == after
    assert before_wind == after_wind, "wind forecast must never change due to operational constraints"


def _setup_with_run(api, fake):
    _setup(api, fake)
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(H24, {"80": [WS_HIGH] * 24, "100": [WS_HIGH] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_missing_inventory_is_explicit_not_fabricated(client):
    api, fake, _s = client
    _setup(api, fake)
    # remove inventory via a NEW empty revision (non-destructive marker)
    api.delete("/api/assets/krnovo/turbines")
    # archive a run WITHOUT regenerating inventory (no _setup call here)
    fake.push_run("gfs_seamless", f"{DAY}T00:00", make_raw_run(H24, {"80": [WS_HIGH] * 24, "100": [WS_HIGH] * 24}))
    r = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": DAY, "run_hour": 0, "days": 16})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    fc = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo", "with_availability": "true"}).json()
    m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
    # adjusted values stay explicitly None — never fabricated zeros
    assert m["adjusted"]["available"] is True
    assert all(p["adjusted_mw"] is None for p in m["adjusted"]["points"])
    st = fc["operational"]
    assert st["inventory_present"] is False
    assert st["n_turbines_inventory"] == 0


def test_addendum_c_285mw_fleet_741_720_695_6915(client):
    """Addendum C: 26 × 2.85 MW = 74.10 MW nameplate (GE curve is 2.78-max →
    explicit scaled-reference opt-in). Owner selects 6 turbines at 2.50 MW
    max → 20 × 2.85 + 6 × 2.50 = 72.00 MW. One of the six OFF → 69.50.
    An UNLIMITED unit OFF instead → 69.15. No default farm cap."""
    api, fake, _s = client
    r = api.post("/api/assets", json={
        "id": "farm-285", "label": "2.85MW fleet", "latitude": 45.0, "longitude": 16.0,
        "capacity_mw": 74.1, "turbine_count": 26, "turbine_rated_mw": 2.85,
        "hub_height_m": 90, "loss_factor": 0.0, "timezone": "Europe/Podgorica",
        "power_curve_id": "ge103-2750-oedb", "allow_scaled_reference": True,
    })
    assert r.status_code == 200, r.text
    g = api.post("/api/assets/farm-285/turbines/generate", json={"count": 26, "rating_mw": 2.85})
    assert g.status_code == 200, g.text
    inv = g.json()
    assert inv["installed_mw"] == pytest.approx(26 * 2.85, abs=1e-6)

    high_day = "2026-11-16"
    fake.push_run("gfs_seamless", f"{high_day}T00:00", make_raw_run(
        [(datetime.fromisoformat(high_day + "T00:00:00+00:00") + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(24)],
        {"80": [WS_HIGH] * 24, "100": [WS_HIGH] * 24}))
    rr = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": high_day, "run_hour": 0, "days": 16, "site_id": "farm-285"})
    run_id = rr.json()["run_id"]

    def _max(site="farm-285"):
        fc = api.get("/api/forecast", params={"days": 2, "site_id": site, "with_availability": "true"}).json()
        m = next(x for x in fc["models"] if x["model_id"] == "gfs_seamless")
        adj = m["adjusted"]
        if not adj.get("available") or not adj.get("points"):
            raise AssertionError(f"adjusted unavailable: reason={adj.get('reason')} | keys={list(adj.keys())}")
        vals = [p["adjusted_mw"] for p in adj["points"] if p["adjusted_mw"] is not None]
        if not vals:
            raise AssertionError(f"all adjusted None: statuses={set(p['status'] for p in adj['points'])}")
        return max(vals)

    assert _max() == pytest.approx(74.1, abs=1e-3), "26 × 2.85 scaled at high wind = 74.10"

    six = [f"WTG-{i:03d}" for i in range(1, 7)]
    _constrain(api, [{"kind": "curtailment", "unit_ids": six, "cap_mw": 2.50, "start_local": "2026-11-12T00:00", "end_local": None}], site="farm-285")
    assert _max() == pytest.approx(20 * 2.85 + 6 * 2.50, abs=1e-3), "72.00 MW"

    # one of the six capped units OFF → 69.50
    c_off = _constrain(api, [{"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-11-12T00:00", "end_local": None}], site="farm-285")
    assert _max() == pytest.approx(20 * 2.85 + 5 * 2.50, abs=1e-3), "69.50 MW"

    # swap: capped unit back ON, an UNLIMITED unit OFF → 69.15
    # (all six caps remain in force: 19×2.85 + 6×2.50; deletions are auditable
    # successor revisions, never destructive history edits)
    off_ids = [c["id"] for c in c_off["constraints"] if c["kind"] == "offline"]  # response = full revision
    api.post("/api/assets/farm-285/constraints/delete", json={"ids": off_ids})
    _constrain(api, [{"kind": "offline", "unit_ids": ["WTG-007"], "start_local": "2026-11-12T00:00", "end_local": None}], site="farm-285")
    assert _max() == pytest.approx(19 * 2.85 + 6 * 2.50, abs=1e-3), "69.15 MW"

    # low wind: no loss from the 2.50 caps when raw < 2.50
    from app.power import load_curve
    ge = load_curve()
    raw3 = ge.power_mw_per_turbine(3.0)  # ~0.017 MW
    low_day = "2026-11-18"  # inside the active constraint window (starts 2026-11-12 local)
    fake.push_run("gfs_seamless", f"{low_day}T00:00", make_raw_run(
        [(datetime.fromisoformat(low_day + "T00:00:00+00:00") + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(24)],
        {"80": [3.0] * 24, "100": [3.0] * 24}))
    rr2 = api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": low_day, "run_hour": 0, "days": 16, "site_id": "farm-285"})
    run_low = rr2.json()["run_id"]
    fc_low = api.get("/api/forecast", params={"days": 2, "site_id": "farm-285", "with_availability": "true"}).json()
    # caps on WTG-001..006 remain in force, WTG-007 OFF: 25 producing units,
    # each below its 2.50 cap → no cap loss anywhere. The GE curve peaks at
    # 2.78 MW (rated_power_mw), so scaled-reference rescales every point by
    # 2.85/2.78 — the caps (2.50) still sit far above raw output.
    raw3_scaled = raw3 * 2.85 / ge.rated_power_mw
    sc = api.get("/api/assets/farm-285/scenario", params={"run_id": run_low}).json()
    first = sc["points"][0]
    assert first["adjusted_mw"] == pytest.approx(25 * raw3_scaled, rel=1e-3), "no cap loss when raw under cap"

    # REFERENCE/UNVERIFIED: scaled-reference must be labelled
    assert sc["summary"]["curve_mode"] == "scaled-reference"
