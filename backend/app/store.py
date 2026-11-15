"""Reading and seeding persisted site/curve configuration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbm
from .power import PowerCurve
from .power import load_curve as load_default_curve
from .registry import KRNOVO_ASSET, SOURCE_PROVIDER


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def curve_row_to_power_curve(row: sqlite3.Row) -> PowerCurve:
    return PowerCurve(
        id=row["id"],
        label=row["label"],
        status=row["status"],
        source=row["source"],
        provenance=row["provenance"],
        rated_power_mw=row["rated_power_mw"],
        cut_in_mps=row["cut_in_mps"],
        rated_mps=row["rated_mps"],
        cut_out_mps=row["cut_out_mps"],
        points=tuple((float(v), float(p)) for v, p in json.loads(row["points_json"])),
    )


def load_curve(db_path, curve_id: str | None = None) -> PowerCurve:
    """Load the curve persisted for the site; fall back to the library default."""
    with dbm.session(db_path) as conn:
        if curve_id:
            row = conn.execute("SELECT * FROM power_curves WHERE id=?", (curve_id,)).fetchone()
            if row:
                return curve_row_to_power_curve(row)
        row = conn.execute("SELECT * FROM power_curves ORDER BY id LIMIT 1").fetchone()
        if row:
            return curve_row_to_power_curve(row)
    return load_default_curve()


def load_site(db_path, site_id: str) -> dict:
    with dbm.session(db_path) as conn:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (site_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown site: {site_id}")
    site = dict(row)
    site["assumptions"] = json.loads(site.pop("assumptions_json") or "{}")
    return site


def load_site_and_curve(db_path, site_id: str) -> tuple[dict, PowerCurve]:
    site = load_site(db_path, site_id)
    curve = load_curve(db_path, site.get("power_curve_id"))
    return site, curve


def seed_defaults(db_path) -> None:
    """Create schema and seed the Krnovo asset + power curve (idempotent)."""
    dbm.init_db(db_path)
    curve = load_default_curve()
    with dbm.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO power_curves (id, label, status, source, provenance, rated_power_mw,
                                      cut_in_mps, rated_mps, cut_out_mps, points_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label, status=excluded.status, source=excluded.source,
                provenance=excluded.provenance, rated_power_mw=excluded.rated_power_mw,
                cut_in_mps=excluded.cut_in_mps, rated_mps=excluded.rated_mps,
                cut_out_mps=excluded.cut_out_mps, points_json=excluded.points_json
            """,
            (
                curve.id,
                curve.label,
                curve.status,
                curve.source,
                curve.provenance,
                curve.rated_power_mw,
                curve.cut_in_mps,
                curve.rated_mps,
                curve.cut_out_mps,
                dbm.j([list(p) for p in curve.points]),
            ),
        )
        conn.execute(
            """
            INSERT INTO assets (id, name, country, latitude, longitude, capacity_mw,
                                turbine_count, turbine_model, turbine_rated_mw, hub_height_m,
                                loss_factor, timezone, power_curve_id, assumptions_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                power_curve_id=excluded.power_curve_id
            """,
            (
                KRNOVO_ASSET["id"],
                KRNOVO_ASSET["name"],
                KRNOVO_ASSET["country"],
                KRNOVO_ASSET["latitude"],
                KRNOVO_ASSET["longitude"],
                KRNOVO_ASSET["capacity_mw"],
                KRNOVO_ASSET["turbine_count"],
                KRNOVO_ASSET["turbine_model"],
                KRNOVO_ASSET["turbine_rated_mw"],
                KRNOVO_ASSET["hub_height_m"],
                KRNOVO_ASSET["loss_factor"],
                KRNOVO_ASSET["timezone"],
                curve.id,
                dbm.j(KRNOVO_ASSET["assumptions"]),
                KRNOVO_ASSET["notes"],
            ),
        )


def ensure_db(db_path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    seed_defaults(db_path)
