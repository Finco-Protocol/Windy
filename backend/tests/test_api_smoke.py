"""API smoke tests: health, site provenance, export CSV, run detail."""

import io

from app.db import SCHEMA_VERSION
from tests.conftest import make_raw_run


def test_health(client):
    api, _fake, settings = client
    h = api.get("/api/health").json()
    assert h["ok"] is True
    assert h["schema_version"] == str(SCHEMA_VERSION)
    assert str(settings.db_path) == h["db_path"]


def test_site_shows_assumption_provenance(client):
    api, _fake, _settings = client
    site = api.get("/api/site").json()
    assert site["site"]["id"] == "krnovo"
    assert site["site"]["capacity_mw"] == 74.1
    assert site["site"]["hub_height_m"] == 90.0
    assumptions = site["site"]["assumptions"]
    assert "PROVISIONAL" in assumptions["hub_height_m"]
    assert "74.10 MW = 26 × 2.85 MW" in assumptions["capacity_mw"]
    assert site["power_curve"]["status"] in ("manufacturer-reference", "demo-illustrative")
    ids = [m["id"] for m in site["models"]]
    assert ids == [
        "ecmwf_ifs", "gfs_seamless", "icon_global", "icon_eu",
        "meteofrance_arpege_world", "meteofrance_arpege_europe", "gem_seamless", "ukmo_seamless",
    ]
    ukmo = next(m for m in site["models"] if m["id"] == "ukmo_seamless")
    assert "MET_ONLY" in ukmo["note"]
    assert "open-meteo" in site["source_note"].lower()


def test_forecast_empty_state_is_explicit(client):
    api, _fake, _settings = client
    fc = api.get("/api/forecast", params={"days": 7}).json()
    assert fc["models"][0]["run"] is None
    assert any("Fetch now" in w for w in fc["models"][0]["warnings"])
    assert fc["intersection"]["n_common_hours"] == 0


def test_run_detail_and_export(client):
    api, fake, _settings = client
    times = [f"2026-11-14T{h:02d}:00" for h in range(3)]
    fake.push_run("gfs_seamless", "2026-11-14T00:00", make_raw_run(times, {"80": [6.0] * 3, "100": [6.4] * 3}))
    api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 0})
    runs = api.get("/api/runs", params={"model_id": "gfs_seamless"}).json()["runs"]
    run_id = runs[0]["id"]

    detail = api.get(f"/api/runs/{run_id}").json()
    assert detail["model_id"] == "gfs_seamless"
    assert len(detail["points"]) == 3
    assert "raw_json" not in detail, "raw payload must not be dumped into the API"

    export = api.get("/api/export/forecast.csv", params={"run_id": run_id})
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    header = export.text.splitlines()[0]
    for col in ("model_id", "run_issued_utc", "valid_at_utc", "wind_speed_mps", "wind_direction_deg", "modeled_power_mw", "interval_energy_mwh", "status"):
        assert col in header
    assert len(export.text.splitlines()) == 4  # header + 3 points


def test_actuals_export(client):
    api, _fake, _settings = client
    csv_bytes = b"ts,mwh\n2026-11-14T00:00:00Z,1.5\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    api.post(
        "/api/actuals/import",
        files={"file": ("a.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "reject"},
    )
    export = api.get("/api/export/actuals.csv")
    assert export.status_code == 200
    lines = export.text.strip().splitlines()
    assert lines[0] == "interval_start_utc,interval_end_utc,energy_mwh,power_mw,import_id"
    assert len(lines) == 2 and "1.5" in lines[1]


# ---- error handlers: expected invalid input never becomes a 500 -------------


def test_error_handlers_return_clean_status_codes(client):
    api, _fake, _settings = client

    # invalid import mapping → 400 (ActualsError)
    r = api.post(
        "/api/actuals/preview",
        files={"file": ("a.csv", io.BytesIO(b"ts,mwh\n2026-11-14T00:00:00Z,1\n"), "text/csv")},
        data={"mapping": '{"timestamp_col":"nope","value_col":"mwh","value_kind":"energy_mwh"}'},
    )
    assert r.status_code == 400
    assert "detail" in r.json()

    # unknown backfill model → 404 (KeyError)
    r = api.post("/api/backfill", json={"model_id": "not_a_model", "run_date": "2026-11-14", "run_hour": 0})
    assert r.status_code == 404

    # invalid run hour / date → 400 (ValueError)
    assert api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "2026-11-14", "run_hour": 99}).status_code == 400
    assert api.post("/api/backfill", json={"model_id": "gfs_seamless", "run_date": "not-a-date", "run_hour": 0}).status_code == 400

    # nonexistent run → 404 (KeyError), via detail and compare
    assert api.get("/api/runs/999999").status_code == 404
    r = api.get("/api/compare", params={"run_id": 999999, "target_date": "2026-11-14"})
    assert r.status_code == 404

    # accuracy with reversed range → 400 (ValueError)
    r = api.get("/api/accuracy", params={"from_utc": "2026-11-15T00:00:00Z", "to_utc": "2026-11-14T00:00:00Z"})
    assert r.status_code == 400


def test_import_blocked_when_rows_fail_validation(client):
    """A file with invalid rows is blocked, never partially imported."""
    api, _fake, settings = client
    csv_bytes = b"ts,mwh\n2026-11-14T00:00:00Z,10\n2026-11-14T01:00:00Z,abc\n"
    mapping = '{"timestamp_col":"ts","value_col":"mwh","value_kind":"energy_mwh","tz":"UTC","interval_hours":1.0}'
    r = api.post(
        "/api/actuals/import",
        files={"file": ("mixed.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["n_errors"] == 1
    assert "blocked" in body["detail"]["message"]

    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    n = conn.execute("SELECT COUNT(*) FROM actual_points").fetchone()[0]
    conn.close()
    assert n == 0, "blocked import must not persist anything"

    # fully valid file still imports normally
    good = b"ts,mwh\n2026-11-14T00:00:00Z,10\n"
    r2 = api.post(
        "/api/actuals/import",
        files={"file": ("good.csv", io.BytesIO(good), "text/csv")},
        data={"mapping": mapping, "on_conflict": "replace"},
    )
    assert r2.status_code == 200 and r2.json()["imported"] is True


# ---- built-frontend serving: SPA deep links + real 404s ----------------------


def test_spa_deep_links_and_static_404s(tmp_path, fake_client):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>WINDY-INDEX</body></html>")
    (dist / "assets" / "app.js").write_text("console.log('windy');")
    (dist / "favicon.ico").write_bytes(b"ico")

    settings = Settings()
    settings.db_path = tmp_path / "spa.db"
    settings.scheduler_enabled = False
    settings.frontend_dist = dist
    application = create_app(settings=settings, om_client=fake_client)
    with TestClient(application) as api:
        # application deep links serve the SPA shell
        for path in ("/", "/history", "/actuals"):
            r = api.get(path)
            assert r.status_code == 200 and "WINDY-INDEX" in r.text
            assert "text/html" in r.headers["content-type"]

        # real static files are served from the build
        r = api.get("/assets/app.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert api.get("/favicon.ico").status_code == 200

        # missing asset chunks stay real 404s — never index.html as JS
        r = api.get("/assets/missing.js")
        assert r.status_code == 404

        # API is untouched: known route 200, unknown route JSON 404
        assert api.get("/api/health").json()["ok"] is True
        r = api.get("/api/not-a-real-route")
        assert r.status_code == 404
