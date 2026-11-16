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


# --- multi-site assets ------------------------------------------------------------


def list_assets(db_path) -> list[dict]:
    """All assets with their curve's site-specificity flag."""
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT a.*, pc.site_id AS curve_site_id, pc.label AS curve_label, pc.status AS curve_status "
            "FROM assets a LEFT JOIN power_curves pc ON pc.id = a.power_curve_id ORDER BY a.id"
        ).fetchall()
    out = []
    for r in rows:
        asset = dict(r)
        assumptions = json.loads(asset.pop("assumptions_json") or "{}")
        asset["assumptions"] = assumptions
        asset["curve_site_specific"] = bool(asset.pop("curve_site_id"))
        asset["curve_reference_only"] = not asset["curve_site_specific"]
        out.append(asset)
    return out


def create_asset(db_path, fields: dict) -> dict:
    """Create a new asset with strict validation.

    The power curve must be chosen explicitly and must exist. Configuration
    that shapes derived MWh (hub height, capacity, turbines, curve, loss) is
    fixed at creation for this phase: there is no edit path, so archived
    snapshots can never be silently relabelled under changed settings.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def _req(name: str):
        v = fields.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError(f"{name} is required")
        return v

    label = str(_req("label")).strip()
    if not any(c.isalnum() for c in label):
        raise ValueError("label must contain at least one alphanumeric character")
    site_id = str(fields.get("id") or "").strip() or _slugify(label)
    if len(site_id) < 2:
        raise ValueError("id is too short — provide an explicit id (>= 2 characters)")
    if not site_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("id must be a simple slug (letters, digits, -, _)")

    lat = _float(fields, "latitude", -90.0, 90.0)
    lon = _float(fields, "longitude", -180.0, 180.0)
    capacity = _float(fields, "capacity_mw", 0.001, 10000.0)
    count_raw = fields.get("turbine_count")
    try:
        turbine_count = int(count_raw)
    except (TypeError, ValueError):
        raise ValueError("turbine_count must be a whole number (never a decimal)") from None
    if turbine_count != float(count_raw) or turbine_count < 1:
        raise ValueError("turbine_count must be a positive whole number (never a decimal)")
    turbine_rated = _float(fields, "turbine_rated_mw", 0.01, 20.0)
    hub = _float(fields, "hub_height_m", 10.0, 300.0)
    loss_raw = fields.get("loss_factor", 0)
    try:
        loss = float(loss_raw)
    except (TypeError, ValueError):
        raise ValueError("loss_factor must be a number") from None
    if not 0.0 <= loss <= 0.999:
        raise ValueError("loss_factor must be between 0.0 and 0.999")
    tz_name = str(fields.get("timezone") or "Europe/Podgorica").strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone: {tz_name!r}") from None

    curve_id = str(_req("power_curve_id")).strip()
    # Nameplate reconciliation: installed capacity (capacity_mw) and
    # count × common rating are separate fields; a disagreement must be
    # explicitly reconciled (mixed-fleet mode), never silently accepted.
    implied = turbine_count * turbine_rated
    reconcile = bool(fields.get("reconcile_nameplate"))
    if abs(implied - capacity) > 0.05 and not reconcile:
        raise ValueError(
            f"installed capacity ({capacity} MW) does not match turbine_count × rating "
            f"({implied} MW). Pass reconcile_nameplate=true ONLY for an explicitly declared "
            "mixed fleet (per-unit ratings will define the true nameplate)."
        )
    allow_scaled = 1 if bool(fields.get("allow_scaled_reference")) else 0
    with dbm.session(db_path) as conn:
        curve = conn.execute(
            "SELECT id, site_id FROM power_curves WHERE id=?", (curve_id,)
        ).fetchone()
        if curve is None:
            raise ValueError(f"power curve '{curve_id}' does not exist — choose one explicitly from /api/power-curves")
        exists = conn.execute("SELECT 1 FROM assets WHERE id=?", (site_id,)).fetchone()
        if exists:
            raise ValueError(f"asset id '{site_id}' already exists")
        conn.execute(
            """
            INSERT INTO assets (id, name, country, latitude, longitude, capacity_mw,
                                turbine_count, turbine_model, turbine_rated_mw, hub_height_m,
                                loss_factor, timezone, power_curve_id, assumptions_json, notes,
                                allow_scaled_reference)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_id,
                label,
                str(fields.get("country") or ""),
                lat,
                lon,
                capacity,
                turbine_count,
                str(fields.get("turbine_model") or ""),
                turbine_rated,
                hub,
                loss,
                tz_name,
                curve_id,
                dbm.j(
                    {
                        "configuration": (
                            "Settings fixed at creation for this phase; archived forecast snapshots "
                            "are never reprocessed or relabelled under changed settings."
                        ),
                        "power_curve": (
                            f"Explicitly selected curve '{curve_id}'"
                            + (" (site-specific)." if curve["site_id"] else " — generic REFERENCE curve, not site-specific; modelled MWh are unverified estimates.")
                        ),
                        "nameplate": (
                            f"installed {capacity} MW declared; count × common rating = {implied} MW"
                            + (" (explicitly reconciled mixed fleet — per-unit turbine inventory defines the true nameplate)" if reconcile and abs(implied - capacity) > 0.05 else "")
                        ),
                    }
                ),
                str(fields.get("notes") or "User-created asset."),
                allow_scaled,
            ),
        )
    return load_site(db_path, site_id)


def _float(fields: dict, name: str, lo: float, hi: float) -> float:
    v = fields.get(name)
    if v is None:
        raise ValueError(f"{name} is required")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= f <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return f


def _slugify(label: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9]+", "-", label.strip().lower()).strip("-")
    return s or "site"


def list_curves(db_path) -> list[dict]:
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, label, status, source, provenance, rated_power_mw, cut_in_mps, rated_mps, cut_out_mps, site_id "
            "FROM power_curves ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c["site_specific"] = bool(c.pop("site_id"))
        out.append(c)
    return out
