"""PR#8 Correction A §3 — the owner-supplied 26 GPS/altitude records.

The committed package backend/app/data/krnovo_turbine_locations.json is
converted VERBATIM from the owner workbook 'Geographical coordinates &
altitude.xlsx' (DMS -> decimal WGS84). These tests import the REAL owner
data (not synthetic fixtures) and prove: 26/26, distinct locations, ranges,
DMS round-trip, append-only revision semantics, restart persistence and API
readback. unit_id WTG-001..026 mapping is PROVISIONAL and asserted as such.
"""

import json
from pathlib import Path

import pytest

from app.siteconfig import get_locations, import_owner_locations, load_owner_locations_package
from app.store import ensure_db
from fastapi.testclient import TestClient  # noqa: F401

RANGE_ELEV_MIN = 1400.0  # Krnovo plateau is ~1400-1700 m ASL
RANGE_ELEV_MAX = 1750.0
RANGE_LAT = (42.0, 43.0)  # Montenegro
RANGE_LON = (18.0, 20.0)


def test_package_is_valid_owner_conversion():
    pkg = load_owner_locations_package()
    assert pkg["site_id"] == "krnovo"
    assert len(pkg["records"]) == 26
    nos = [r["source_no"] for r in pkg["records"]]
    assert nos == list(range(1, 27)), "records sorted by owner source_no 1..26, no gaps"
    assert len({(r["latitude"], r["longitude"]) for r in pkg["records"]}) == 26, "all 26 locations distinct"
    for r in pkg["records"]:
        assert RANGE_LAT[0] < r["latitude"] < RANGE_LAT[1]
        assert RANGE_LON[0] < r["longitude"] < RANGE_LON[1]
        assert RANGE_ELEV_MIN < r["elevation_m"] < RANGE_ELEV_MAX
        # DMS provenance retained for row-by-row verification
        d, m, s = r["dms_original"].split(" | ")[0].split()
        rebuilt = int(d) + int(m) / 60 + float(s) / 3600
        assert rebuilt == pytest.approx(r["latitude"], abs=1e-9), "DMS -> decimal round-trip exact"
        d, m, s = r["dms_original"].split(" | ")[1].split()
        rebuilt = int(d) + int(m) / 60 + float(s) / 3600
        assert rebuilt == pytest.approx(r["longitude"], abs=1e-9)
    # provisional mapping is explicit in the package provenance
    assert "PROVISIONAL" in pkg["source"]["id_mapping"]


def test_import_creates_revision_26_owner_records(client):
    api, _fake, settings = client
    out = import_owner_locations(str(settings.db_path), "krnovo")
    assert out["revision"] == 1
    assert out["n_locations"] == 26
    got = get_locations(str(settings.db_path), "krnovo")
    assert got["revision"] == 1 and got["n_locations"] == 26
    assert got["locations"][0]["unit_id"] == "WTG-001"
    assert got["locations"][-1]["unit_id"] == "WTG-026"
    assert got["revision_history"][0]["n_records"] == 26
    # every record carries the owner provenance marker
    assert all(loc["source"].startswith("owner-xlsx 2026-11-17") for loc in got["locations"])
    # first owner row verbatim: 42 53 41.426450 19 06 28.956895 1509.4318
    first = got["locations"][0]
    assert first["latitude"] == pytest.approx(42 + 53 / 60 + 41.426450 / 3600, abs=1e-9)
    assert first["longitude"] == pytest.approx(19 + 6 / 60 + 28.956895 / 3600, abs=1e-9)
    assert first["elevation_m"] == pytest.approx(1509.4318, abs=1e-9)

    # RESTART persists (append-only storage)
    ensure_db(settings.db_path)
    got2 = get_locations(str(settings.db_path), "krnovo")
    assert got2 == got


def test_reimport_appends_new_revision_never_rewrites(client):
    api, _fake, settings = client
    import_owner_locations(str(settings.db_path), "krnovo")
    out2 = import_owner_locations(str(settings.db_path), "krnovo")
    assert out2["revision"] == 2, "re-import appends revision 2, never rewrites revision 1"
    got = get_locations(str(settings.db_path), "krnovo")
    assert got["revision"] == 2 and got["n_locations"] == 26
    assert [h["revision"] for h in got["revision_history"]] == [1, 2]


def test_locations_visible_through_api(client):
    api, _fake, settings = client
    import_owner_locations(str(settings.db_path), "krnovo")
    r = api.get("/api/assets/krnovo/locations")
    assert r.status_code == 200
    out = r.json()
    assert out["n_locations"] == 26
    lats = [loc["latitude"] for loc in out["locations"]]
    lons = [loc["longitude"] for loc in out["locations"]]
    elevs = [loc["elevation_m"] for loc in out["locations"]]
    assert 42.86 < min(lats) and max(lats) < 42.90
    assert 19.09 < min(lons) and max(lons) < 19.14
    assert 1473 <= min(elevs) and max(elevs) <= 1670
    # centroid close to the single farm point used for weather queries
    assert abs(sum(lats) / 26 - 42.8756) < 0.01
    assert abs(sum(lons) / 26 - 19.1106) < 0.01
