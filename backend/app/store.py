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
    """Load the curve persisted for the site.

    An asset that references a curve id which is missing from `power_curves`
    is a data-integrity problem: it fails explicitly instead of silently
    falling back to an unrelated (alphabetically first) curve. The default
    library curve is used only when the asset carries no reference at all
    (first-startup path before seeding).
    """
    with dbm.session(db_path) as conn:
        if curve_id:
            row = conn.execute("SELECT * FROM power_curves WHERE id=?", (curve_id,)).fetchone()
            if row:
                return curve_row_to_power_curve(row)
            raise KeyError(f"power curve '{curve_id}' referenced by the site is missing from power_curves")
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
    """Create schema and seed defaults ONLY if absent (idempotent, non-destructive).

    The Krnovo asset and the default power curve are first-startup seeds:
    user-edited asset values (hub height, losses, curve reference) and edited
    curve data are preserved across restarts, CLI init and scheduler startup.
    Configuration evolution must go through versioned migrations, never an
    implicit reset-to-default.
    """
    dbm.init_db(db_path)
    with dbm.session(db_path) as conn:
        already_seeded = conn.execute("SELECT 1 FROM assets WHERE id=?", (KRNOVO_ASSET["id"],)).fetchone()
    if already_seeded:
        return
    curve = load_default_curve()
    with dbm.session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO power_curves (id, label, status, source, provenance, rated_power_mw,
                                      cut_in_mps, rated_mps, cut_out_mps, points_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
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
            ON CONFLICT(id) DO NOTHING
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
