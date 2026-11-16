"""Settings-persistence tests (P0): first-startup seeds, non-destructive reseeds,
explicit error for a broken curve reference."""

import pytest

from app import db as dbm
from app.store import ensure_db, load_curve, load_site, load_site_and_curve, seed_defaults


def test_first_startup_seeds_asset_and_curve(tmp_path):
    db = tmp_path / "w.db"
    ensure_db(db)
    site = load_site(db, "krnovo")
    assert site["capacity_mw"] == 71.5
    assert site["hub_height_m"] == 90.0
    curve = load_curve(db, site["power_curve_id"])
    assert curve.id == "ge103-2750-oedb"
    assert len(curve.points) == 51


def test_reseed_does_not_overwrite_user_edits(tmp_path):
    db = tmp_path / "w.db"
    ensure_db(db)

    # user edits: hub height, loss factor, turbine model, curve data, curve ref
    with dbm.session(db) as conn:
        conn.execute("UPDATE assets SET hub_height_m=95.0, loss_factor=0.07, turbine_model='EDITED-MODEL' WHERE id='krnovo'")
        conn.execute(
            "UPDATE power_curves SET rated_power_mw=3.0, points_json='[[0.0,0.0],[30.0,3.0]]' WHERE id='ge103-2750-oedb'"
        )

    # "restart": ensure_db + explicit seed_defaults must change nothing
    ensure_db(db)
    seed_defaults(db)

    site = load_site(db, "krnovo")
    assert site["hub_height_m"] == 95.0
    assert site["loss_factor"] == 0.07
    assert site["turbine_model"] == "EDITED-MODEL"
    curve = load_curve(db, site["power_curve_id"])
    assert curve.rated_power_mw == 3.0
    assert curve.points == ((0.0, 0.0), (30.0, 3.0))


def test_unknown_curve_reference_fails_explicitly(tmp_path):
    db = tmp_path / "w.db"
    ensure_db(db)
    with dbm.session(db) as conn:
        conn.execute("UPDATE assets SET power_curve_id='missing-curve' WHERE id='krnovo'")
    with pytest.raises(KeyError, match="missing-curve"):
        load_curve(db, "missing-curve")
    with pytest.raises(KeyError, match="missing-curve"):
        load_site_and_curve(db, "krnovo")


def test_default_curve_is_not_used_when_asset_has_reference(tmp_path):
    """The library default may only serve the pre-seed path, never override a
    stored reference with an unrelated curve."""
    db = tmp_path / "w.db"
    ensure_db(db)
    site = load_site(db, "krnovo")
    curve = load_curve(db, site["power_curve_id"])
    assert curve.id == site["power_curve_id"]
