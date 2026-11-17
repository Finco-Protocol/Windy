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
import json
from zoneinfo import ZoneInfo
from pathlib import Path

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
    """Correction B §2 — same-site fleet reconfiguration that CANNOT leave the
    assets row and the turbine inventory inconsistent.

    Everything is PREVALIDATED before any write:
    - whole-number turbine count, finite numbers in range, known curve;
    - a physical fleet change (turbine_count / turbine_rated_mw) either
      matches the EXISTING inventory already, or explicitly requires
      `regenerate_inventory=true` (otherwise rejected with an actionable
      error — never a silent 74.10-in-assets / 71.50-in-inventory split);
    - when regenerating, effective installed MW must equal effective
      capacity_mw (no silent mixed-fleet reconciliation in this step);
    - a rated-vs-curve mismatch REQUIRES the explicit allow_scaled_reference
      (REFERENCE/UNVERIFIED) opt-in — never a silent default opt-in;
    - ACTIVE constraints referencing units that would disappear under a
      smaller fleet REJECT the revision with their ids listed (constraints
      are never silently discarded).

    The assets UPDATE, the NEW inventory revision rows and the config
    revision row are written in ONE SQLite transaction (rolled back entirely
    on any failure). Archived runs, constraints, actuals and published
    scenario revisions are untouched.
    """
    from .availability import (
        AvailabilityError,
        current_constraints_revision,
        current_inventory_revision,
        get_constraints,
        get_inventory,
    )
    from .store import load_curve as _store_load_curve

    site = load_site(db_path, site_id)
    if not isinstance(changes, dict) or not changes:
        raise AvailabilityError("changes must be a non-empty object")

    changes = dict(changes)
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
                if abs(num - round(num)) > 1e-9:
                    raise AvailabilityError(f"turbine_count must be a whole number, got {value}")
                num = int(round(num))
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

    # Correction C §1: judge REAL change by VALUES, not by key presence — a
    # client may send unchanged count/rating alongside a hub-height tweak.
    def _same_value(new, old) -> bool:
        if isinstance(new, (int, float)) and isinstance(old, (int, float)):
            return abs(float(new) - float(old)) <= 1e-9
        return new == old

    changed = {k: v for k, v in validated.items() if not _same_value(v, old_vals.get(k))}
    if not changed:
        return {
            "site_id": site_id,
            "no_op": True,
            "revision": current_config_revision(db_path, site_id),
            "changes": {},
            "effective_from_utc": None,
            "note": "nema stvarne promjene vrijednosti — revizija nije kreirana",
            "inventory_revision": None,
            "regenerated_inventory": False,
            "constraint_impact": [],
            "site": site,
        }

    # ---- prevalidation of the COMPLETE target configuration -----------------
    target_count = int(validated.get("turbine_count", site.get("turbine_count") or 0))
    target_rating = float(validated.get("turbine_rated_mw", site.get("turbine_rated_mw") or 0))
    target_capacity = float(validated.get("capacity_mw", site.get("capacity_mw") or 0))
    target_curve_id = validated.get("power_curve_id", site.get("power_curve_id"))
    target_allow_scaled = int(validated.get("allow_scaled_reference", site.get("allow_scaled_reference") or 0))
    if target_curve_id is not None:
        with dbm.session(db_path) as conn:
            row = conn.execute("SELECT 1 FROM power_curves WHERE id=?", (target_curve_id,)).fetchone()
            if row is None:
                raise AvailabilityError(f"unknown power curve: {target_curve_id}")

    inv = get_inventory(db_path, site_id)
    # Correction C §1: fleet change is decided by VALUES vs the stored asset —
    # unchanged count/rating keys (e.g. a hub-height-only edit that echoes the
    # whole form) must NOT demand regeneration
    fleet_changed = (
        abs(float(target_count) - float(site.get("turbine_count") or 0)) > 1e-9
        or abs(float(target_rating) - float(site.get("turbine_rated_mw") or 0)) > 1e-9
    )
    inventory_matches_target = (
        inv["n_turbines"] == target_count
        and abs(inv["installed_mw"] - target_count * target_rating) <= 0.05
    )
    # Correction B: the flag is the explicit owner consent to rebuild the
    # fleet — a fleet change WITHOUT it is rejected below, never auto-regenerated
    regenerating = regenerate

    constraint_impact: list[str] = []
    if regenerating:
        installed = target_count * target_rating
        if abs(installed - target_capacity) > 0.05:
            raise AvailabilityError(
                f"physical fleet change rejected: {target_count} x {target_rating} MW = "
                f"{round(installed, 2)} MW installed, but effective capacity_mw is {target_capacity} MW. "
                "Set capacity_mw to the matching nameplate (or change it in the same revision)."
            )
        curve = _store_load_curve(db_path, target_curve_id) if target_curve_id else None
        if curve is not None and curve.rated_power_mw:
            mismatch = abs(float(curve.rated_power_mw) - target_rating) / target_rating
            if mismatch > 0.02 and not target_allow_scaled:
                raise AvailabilityError(
                    f"curve '{target_curve_id}' peaks at {curve.rated_power_mw} MW vs unit rating "
                    f"{target_rating} MW — set allow_scaled_reference=true to explicitly accept the "
                    "scaled REFERENCE/UNVERIFIED curve (no silent opt-in)."
                )
        if target_count < inv["n_turbines"]:
            surviving = {f"WTG-{i:03d}" for i in range(1, target_count + 1)}
            cons = get_constraints(db_path, site_id)
            affected = [c for c in cons["constraints"] if c.get("unit_ids") and any(u not in surviving for u in c["unit_ids"])]
            if affected:
                listed = ", ".join(f"#{c['id']}:{'/'.join(u for u in c['unit_ids'] if u not in surviving)}" for c in affected[:5])
                raise AvailabilityError(
                    f"fleet shrink rejected: {len(affected)} active constraint(s) reference units beyond "
                    f"WTG-{target_count:03d} ({listed}). Remove or re-target those constraints first — "
                    "they are never silently discarded."
                )
        if fleet_changed:
            constraint_impact.append(
                f"inventory regenerates as WTG-001..{target_count:03d} @ {target_rating} MW (new inventory revision); "
                "existing active constraints on surviving units are PRESERVED unchanged "
                "(cap_percent now refers to the new rated MW; verify cap_mw <= new rating)"
            )
    elif fleet_changed:
        raise AvailabilityError(
            f"physical fleet change (turbine count/rating) requires regenerate_inventory=true: current "
            f"inventory is {inv['n_turbines']} units / {inv['installed_mw']} MW, target is {target_count} units / "
            f"{round(target_count * target_rating, 2)} MW. Include regenerate_inventory=true, or align the change "
            "with the existing inventory."
        )
    if "capacity_mw" in changed and not regenerating:
        # Correction C §2: a bare capacity edit (no fleet regeneration) must
        # not desynchronise the asset from the physical inventory, and an
        # OPERATIONAL limit (six turbines at 2.50 MW => 72.00 MW available)
        # never belongs in capacity_mw — it lives in per-unit constraints /
        # an optional export cap. No hardcoded 72 MW anywhere.
        effective_installed = inv["installed_mw"] if inv["n_turbines"] > 0 else target_count * target_rating
        if abs(target_capacity - effective_installed) > 0.05:
            raise AvailabilityError(
                f"capacity_mw je INSTALIRANI nazivni kapacitet: fizička flota je "
                f"{round(effective_installed, 2)} MW, pa je capacity_mw={target_capacity} MW odbijeno. "
                "Operativni limit (npr. šest turbina na 2,50 MW => 72,00 MW raspoloživo) "
                "NE ide u capacity_mw — koristite per-turbine limite u 'Rad elektrane' "
                "ili eksplicitni farm export cap."
            )
    # ---- ONE transaction: assets + (optional) new inventory + revision row ---
    rev = current_config_revision(db_path, site_id) + 1
    inventory_revision: int | None = None
    now = _now()
    changes_json = dbm.j({k: {"from": old_vals.get(k), "to": v} for k, v in changed.items()})
    with dbm.session(db_path) as conn:
        sets = ", ".join(f"{k}=?" for k in changed)
        conn.execute(
            f"UPDATE assets SET {sets} WHERE id=?",
            (*changed.values(), site_id),
        )
        if regenerating:
            inventory_revision = current_inventory_revision(db_path, site_id) + 1
            for i in range(1, target_count + 1):
                conn.execute(
                    "INSERT INTO turbines (site_id, unit_id, name, rated_mw, curve_id, config_revision, created_at_utc) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (site_id, f"WTG-{i:03d}", f"WTG {i:03d}", target_rating, inventory_revision, now),
                )
        conn.execute(
            "INSERT INTO site_config_revisions (site_id, revision, changes_json, effective_from_utc, note, created_at_utc, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'owner')",
            (site_id, rev, changes_json, now, note, now),
        )
        # Correction D §5: CURRENT owner-revised provenance label. The old
        # assumptions keys stay untouched (historical source preserved); the
        # new key states only what the owner actually confirmed (2.85 MW
        # rating) and keeps the curve REFERENCE/UNVERIFIED — never a claim
        # of a manufacturer-certified GE 2.85-116 curve or an invented rotor.
        fleet_fields = {"capacity_mw", "turbine_count", "turbine_rated_mw"} & changed.keys()
        if fleet_fields:
            row2 = conn.execute("SELECT assumptions_json FROM assets WHERE id=?", (site_id,)).fetchone()
            try:
                assumptions = json.loads(row2["assumptions_json"] or "{}")
            except Exception:
                assumptions = {}
            assumptions[f"owner_revision_{rev}"] = (
                f"Vlasnicka revizija {rev} ({now}): "
                + ", ".join(f"{k}={changed[k]}" for k in sorted(fleet_fields))
                + ". Potvrdio vlasnik samo rating 2,85 MW po turbini; krivulja ostaje REFERENCE/UNVERIFIED "
                "(skalirana ge103-2750) i NIJE proizvodjacki certificirana GE 2.85-116 krivulja; rotor/model "
                "varijanta NIJE potvrdena."
            )
            conn.execute("UPDATE assets SET assumptions_json=? WHERE id=?", (dbm.j(assumptions), site_id))
    _ = current_constraints_revision  # kept for API-surface parity
    out = {
        "site_id": site_id,
        "revision": rev,
        "changes": {k: {"from": old_vals.get(k), "to": v} for k, v in changed.items()},
        "effective_from_utc": now,
        "note": note,
        "inventory_revision": inventory_revision,
        "regenerated_inventory": regenerating,
        "constraint_impact": constraint_impact,
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


OWNER_LOCATIONS_PACKAGE = Path(__file__).parent / "ownerdata" / "krnovo_turbine_locations.json"


def load_owner_locations_package() -> dict:
    """The owner-supplied 26 GPS/altitude records (Correction A §3), converted
    verbatim from 'Geographical coordinates & altitude.xlsx' (DMS -> decimal,
    WGS84) into a committed package with provenance. unit_id mapping is
    PROVISIONAL (WTG-{source_no:03d} by owner row order)."""
    import json

    return json.loads(OWNER_LOCATIONS_PACKAGE.read_text(encoding="utf-8"))


def import_owner_locations(db_path, site_id: str, note: str | None = None) -> dict:
    """Append ONE location revision with the owner's 26 records. Reversible by
    design (previous revisions stay queryable); call is idempotent only in the
    sense that a re-import creates revision N+1 — it never rewrites N."""
    pkg = load_owner_locations_package()
    records = [
        {
            "unit_id": r["unit_id"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "elevation_m": r["elevation_m"],
            "source": "owner-xlsx 2026-11-17 (DMS->decimal; WTG mapping provisional)",
        }
        for r in pkg["records"]
    ]
    out = set_locations_revision(db_path, site_id, records, note or note_default(pkg))
    out["source_file"] = pkg["source"]["file"]
    return out


def note_default(pkg: dict) -> str:
    return "Vlasnički podaci (26 zapisa) iz 'Geographical coordinates & altitude.xlsx'; WTG mapiranje privremeno"
