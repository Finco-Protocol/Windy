"""Same-site configuration revisions and per-turbine location records.

PR#8 Correction A §2.1 / §3 / §6: configuration evolution for an EXISTING
site goes through append-only, auditable revisions — never an in-place
silent rewrite and never an implicit reseed on restart. Historical forecast
runs keep their stored values, so provenance is retained:

- `site_config_revisions` records old→new values with an effective-from
  timestamp; new collections are attributed to the new configuration while
  archived runs remain byte-identical.
- `turbine_locations` stores the per-unit GPS/altitude records as versioned
  revisions with source provenance. Import is owner-data driven — NO
  coordinates are invented or seeded here (missing owner input is reported,
  not fabricated).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from .availability import AvailabilityError

# fields an owner may change through a revision, with validators.
# capacity/turbine values are checked numerically; timezone must be a real
# IANA zone; power_curve_id must exist; allow_scaled_reference is the
# explicit REFERENCE/UNVERIFIED opt-in.
EDITABLE_TEXT = {"name", "country", "turbine_model", "timezone", "power_curve_id"}
EDITABLE_NUMBER = {
    "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0),
    "capacity_mw": (0.1, 10000.0),
    "turbine_count": (1, 500),
    "turbine_rated_mw": (0.05, 20.0),
    "hub_height_m": (10.0, 300.0),
    "loss_factor": (0.0, 0.999),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_config_revision(db_path, site_id: str) -> int:
    with dbm.session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM site_config_revisions WHERE site_id=?",
            (site_id,),
        ).fetchone()
        return int(row["r"])


def apply_config_revision(db_path, site_id: str, changes: dict, note: str | None = None) -> dict:
    """Apply owner-verified configuration changes to an EXISTING site in ONE
    auditable revision: the assets row is updated and the old→new diff is
    appended to site_config_revisions in the same transaction. Optional
    `regenerate_inventory` rebuilds the turbine inventory at the new
    turbine_rated_mw as a NEW inventory revision (old one stays queryable).
    Archived runs are never modified."""
    site = load_site(db_path, site_id)
    if not isinstance(changes, dict) or not changes:
        raise AvailabilityError("changes must be a non-empty object")

    regenerate = bool(changes.pop("regenerate_inventory", False))
    validated: dict = {}
    old_vals: dict = {}
    for key, value in changes.items():
        if key in EDITABLE_NUMBER:
            lo, hi = EDITABLE_NUMBER[key]
            try:
                num = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise AvailabilityError(f"{key} must be a number") from None
            if not (lo <= num <= hi):
                raise AvailabilityError(f"{key}={num} outside allowed range [{lo}, {hi}]")
            if key == "turbine_count":
                num = int(num)
            validated[key] = num
            old_vals[key] = site.get(key)
        elif key in EDITABLE_TEXT:
            if not isinstance(value, str) or not value.strip():
                raise AvailabilityError(f"{key} must be a non-empty string")
            if key == "timezone":
                try:
                    ZoneInfo(value)
                except Exception:
                    raise AvailabilityError(f"timezone '{value}' is not a valid IANA zone") from None
            validated[key] = value.strip()
            old_vals[key] = site.get(key)
        elif key == "allow_scaled_reference":
            validated[key] = 1 if value else 0
            old_vals[key] = int(site.get("allow_scaled_reference") or 0)
        else:
            raise AvailabilityError(f"unknown or non-editable field: {key}")

    if "timezone" in validated or "power_curve_id" in validated:
        # never leave the site pointing at a missing curve
        with dbm.session(db_path) as conn:
            target_curve = validated.get("power_curve_id", site.get("power_curve_id"))
            if target_curve is not None:
                row = conn.execute("SELECT 1 FROM power_curves WHERE id=?", (target_curve,)).fetchone()
                if row is None:
                    raise AvailabilityError(f"unknown power curve: {target_curve}")

    rev = current_config_revision(db_path, site_id) + 1
    now = _now()
    changes_json = dbm.j({k: {"from": old_vals.get(k), "to": v} for k, v in validated.items()})
    with dbm.session(db_path) as conn:
        sets = ", ".join(f"{k}=?" for k in validated)
        conn.execute(
            f"UPDATE assets SET {sets} WHERE id=?",
            (*validated.values(), site_id),
        )
        conn.execute(
            "INSERT INTO site_config_revisions (site_id, revision, changes_json, effective_from_utc, note, created_at_utc, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'owner')",
            (site_id, rev, changes_json, now, note, now),
        )
    inventory_revision = None
    if regenerate:
        from .availability import current_inventory_revision, generate_inventory

        rating = float(validated.get("turbine_rated_mw", site.get("turbine_rated_mw") or 0))
        count = int(validated.get("turbine_count", site.get("turbine_count") or 0))
        if count < 1 or rating <= 0:
            raise AvailabilityError("regenerate_inventory needs turbine_count and turbine_rated_mw")
        inv = generate_inventory(db_path, site_id, count, rating)
        inventory_revision = current_inventory_revision(db_path, site_id)
        _ = inv
    out = {
        "site_id": site_id,
        "revision": rev,
        "changes": {k: {"from": old_vals.get(k), "to": v} for k, v in validated.items()},
        "effective_from_utc": now,
        "note": note,
        "inventory_revision": inventory_revision,
        "site": load_site(db_path, site_id),
    }
    return out


def list_config_revisions(db_path, site_id: str) -> dict:
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, revision, changes_json, effective_from_utc, note, created_at_utc, created_by "
            "FROM site_config_revisions WHERE site_id=? ORDER BY revision",
            (site_id,),
        ).fetchall()
    import json as _json

    return {
        "site_id": site_id,
        "current_revision": current_config_revision(db_path, site_id),
        "revisions": [
            {
                "id": r["id"],
                "revision": r["revision"],
                "changes": _json.loads(r["changes_json"]),
                "effective_from_utc": r["effective_from_utc"],
                "note": r["note"],
                "created_at_utc": r["created_at_utc"],
                "created_by": r["created_by"],
            }
            for r in rows
        ],
    }


def current_locations_revision(db_path, site_id: str) -> int:
    with dbm.session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM turbine_locations WHERE site_id=?",
            (site_id,),
        ).fetchone()
        return int(row["r"])


def set_locations_revision(db_path, site_id: str, records: list[dict], note: str | None = None) -> dict:
    """Append a NEW location revision with ALL provided records (full-set
    replacement semantics per revision; previous revisions stay queryable).
    Validation: unique unit_ids, lat/lon ranges, optional elevation. The
    caller is responsible for sourcing real owner data — this function never
    invents coordinates."""
    load_site(db_path, site_id)
    if not isinstance(records, list) or not records:
        raise AvailabilityError("records must be a non-empty list")
    if len(records) > 500:
        raise AvailabilityError("a single request may carry at most 500 records")
    seen: set[str] = set()
    cleaned: list[dict] = []
    for rec in records:
        unit = str(rec.get("unit_id") or "").strip()
        if not unit:
            raise AvailabilityError("every record needs unit_id")
        if unit in seen:
            raise AvailabilityError(f"duplicate unit_id in payload: {unit}")
        seen.add(unit)
        try:
            lat = float(rec["latitude"])  # type: ignore[arg-type]
            lon = float(rec["longitude"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            raise AvailabilityError(f"{unit}: latitude and longitude are required numbers") from None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise AvailabilityError(f"{unit}: coordinates out of range ({lat}, {lon})")
        elev = rec.get("elevation_m")
        elev_f = None
        if elev is not None:
            try:
                elev_f = float(elev)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise AvailabilityError(f"{unit}: elevation_m must be a number") from None
        cleaned.append(
            {"unit_id": unit, "latitude": lat, "longitude": lon, "elevation_m": elev_f, "source": str(rec.get("source") or "owner")}
        )

    rev = current_locations_revision(db_path, site_id) + 1
    now = _now()
    with dbm.session(db_path) as conn:
        for rec in cleaned:
            conn.execute(
                "INSERT INTO turbine_locations (site_id, revision, unit_id, latitude, longitude, elevation_m, source, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, rev, rec["unit_id"], rec["latitude"], rec["longitude"], rec["elevation_m"], rec["source"], now),
            )
    return get_locations(db_path, site_id)


def get_locations(db_path, site_id: str) -> dict:
    with dbm.session(db_path) as conn:
        rev = current_locations_revision(db_path, site_id)
        rows = conn.execute(
            "SELECT unit_id, latitude, longitude, elevation_m, source, created_at_utc "
            "FROM turbine_locations WHERE site_id=? AND revision=? ORDER BY unit_id",
            (site_id, rev),
        ).fetchall() if rev else []
        history = conn.execute(
            "SELECT revision, COUNT(*) AS n, MIN(created_at_utc) AS created FROM turbine_locations WHERE site_id=? GROUP BY revision ORDER BY revision",
            (site_id,),
        ).fetchall()
    return {
        "site_id": site_id,
        "revision": rev,
        "n_locations": len(rows),
        "locations": [dict(r) for r in rows],
        "revision_history": [{"revision": h["revision"], "n_records": h["n"], "created_at_utc": h["created"]} for h in history],
        "note": None if rev else "nema unesenih lokacija — potrebni vlasnički podaci (XLSX/CSV)",
    }


def load_site(db_path, site_id: str) -> dict:
    from .store import load_site as _load

    return _load(db_path, site_id)
