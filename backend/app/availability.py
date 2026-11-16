"""Turbine inventory, operational constraints and availability-adjusted scenarios.

Physical meaning kept strictly separate:

- **Installed nameplate** (asset `capacity_mw`, per-unit `rated_mw`) is a static
  physical configuration — versioned via inventory revisions.
- **Availability / curtailment** (offline, per-unit MW cap, farm export cap)
  are TIME-DEPENDENT operating constraints — versioned via constraint
  revisions. A "50% cap" means `0.5 × rated_MW` as a MAXIMUM permitted output
  (1.375 MW for a 2.75 MW unit), never a multiplier of instantaneous output.

Per forecast interval t and turbine i (algorithm version "1"):

    base_i(t)      = compatible_curve_i(wind(t)) × (1 − site loss factor)
    adjusted_i(t)  = 0                                      if OFFLINE applies
                   = min(base_i(t), most_restrictive_active_cap_MW_i, rated_MW_i)
                     (no cap → min(base_i(t), rated_MW_i))
    pre_export(t)  = Σ_i adjusted_i(t)
    adjusted(t)    = min(pre_export(t), active farm export cap | installed_MW)
    lost_unit(t)   = max(Σ min(base_i(t), rated_i) − pre_export(t), 0) × interval_hours
    lost_export(t) = max(pre_export(t) − adjusted(t), 0) × interval_hours

Losses are applied exactly once (in `base_i`, matching the v1 baseline
`farm_power_mw`), and the per-unit nameplate clamp is treated as part of the
machine (not an operational restriction), so all-turbines-full + no export cap
reproduces the baseline farm estimate within tolerance AND the identity
base_mw − lost_unit − lost_export == adjusted holds against the stored
baseline. Missing wind or an incompatible curve yields `null` outputs and
`null` losses — never zero, never invented.

Revisions are append-only: an inventory or constraint edit creates a NEW
revision (copy-forward of unchanged rows) so any published scenario remains
reproducible bit-for-bit from retained immutable inputs. Local times entered by
the user are converted with the site IANA timezone; DST-nonexistent local times
are rejected and ambiguous ones resolved to the FIRST occurrence with a
recorded warning (same semantics as the actuals importer).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db as dbm
from .collect import iso_z, parse_utc

ALGORITHM_VERSION = "1"
KINDS = ("offline", "curtailment", "export_cap")
TOLERANCE = 1e-9
# Direct curve mode requires the reference curve's rated power to match the
# unit's rated power within this narrow relative tolerance; any larger mismatch
# in EITHER direction must be an explicit opt-in scaled-reference or an
# incompatible-curve null — never a silent clip or reuse.
CURVE_MATCH_TOLERANCE = 0.02


class AvailabilityError(ValueError):
    """Invalid inventory/constraint input (mapped to HTTP 400)."""


# --- time helpers ---------------------------------------------------------------


def local_to_utc(naive_iso: str, tz_name: str) -> tuple[str, str | None]:
    """Convert a naive local timestamp to canonical UTC with explicit DST
    handling. Returns (utc_iso, warning | None). Raises AvailabilityError for
    nonexistent (spring-forward) local times."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise AvailabilityError(f"unknown timezone: {tz_name!r}") from None
    try:
        naive = datetime.fromisoformat(naive_iso)
    except ValueError as exc:
        raise AvailabilityError(f"invalid local time {naive_iso!r}") from exc
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None
    aware = naive.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != naive:
        raise AvailabilityError(
            f"local time {naive_iso!r} does not exist in {tz_name} (DST spring-forward gap)"
        )
    fold1 = naive.replace(tzinfo=tz, fold=1)
    warning = None
    if aware.utcoffset() != fold1.utcoffset():
        warning = f"ambiguous local time {naive_iso!r} in {tz_name}; interpreted as the FIRST occurrence"
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), warning


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(v, name: str, lo: float, hi: float | None = None) -> float:
    import math

    try:
        f = float(v)
    except (TypeError, ValueError):
        raise AvailabilityError(f"{name} must be a number") from None
    if not math.isfinite(f):
        raise AvailabilityError(f"{name} must be finite (no NaN/inf)")
    if f < lo or (hi is not None and f > hi):
        raise AvailabilityError(f"{name} must be in [{lo}, {hi if hi is not None else '∞'}]")
    return f


# --- inventory --------------------------------------------------------------------


def current_inventory_revision(db_path, site_id: str) -> int:
    with dbm.session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(config_revision), 0) AS r FROM turbines WHERE site_id=?", (site_id,)
        ).fetchone()
    return int(row["r"])


def get_inventory(db_path, site_id: str, revision: int | None = None) -> dict:
    rev = revision if revision is not None else current_inventory_revision(db_path, site_id)
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT unit_id, name, rated_mw, curve_id, created_at_utc FROM turbines "
            "WHERE site_id=? AND config_revision=? ORDER BY unit_id",
            (site_id, rev),
        ).fetchall()
    turbines = [dict(r) for r in rows if not r["unit_id"].startswith("__empty__")]
    installed = round(sum(t["rated_mw"] for t in turbines), 6)
    return {
        "site_id": site_id,
        "revision": rev,
        "n_turbines": len(turbines),
        "installed_mw": installed,
        "turbines": turbines,
    }


def generate_inventory(db_path, site_id: str, count: int, rating_mw: float, unit_prefix: str = "WTG") -> dict:
    """Create the initial inventory (or regenerate at a NEW revision) from a
    count + common rating. Append-only: the previous revision stays intact."""
    site = _load_site_tz(db_path, site_id)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 2000:
        raise AvailabilityError("count must be a positive integer (1..2000)")
    rating = _finite(rating_mw, "rating_mw", 0.01, 20.0)
    rev = current_inventory_revision(db_path, site_id) + 1
    now = _now()
    prefix = (unit_prefix or "WTG").strip() or "WTG"
    with dbm.session(db_path) as conn:
        for i in range(1, count + 1):
            conn.execute(
                "INSERT INTO turbines (site_id, unit_id, name, rated_mw, curve_id, config_revision, created_at_utc) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (site_id, f"{prefix}-{i:03d}", f"{prefix} {i:03d}", rating, rev, now),
            )
    return get_inventory(db_path, site_id, rev)


def bulk_edit_ratings(db_path, site_id: str, edits: list[dict]) -> dict:
    """Apply per-unit rated-MW changes in ONE request: creates a NEW inventory
    revision (copy-forward + edits). edits: [{unit_id, rated_mw}]."""
    site = _load_site_tz(db_path, site_id)
    if not isinstance(edits, list) or not edits:
        raise AvailabilityError("edits must be a non-empty list")
    clean: dict[str, float] = {}
    for e in edits:
        unit = str(e.get("unit_id") or "").strip()
        if not unit:
            raise AvailabilityError("each edit needs unit_id")
        clean[unit] = _finite(e.get("rated_mw"), f"rated_mw for {unit}", 0.01, 20.0)
    rev_old = current_inventory_revision(db_path, site_id)
    current = {t["unit_id"]: t for t in get_inventory(db_path, site_id, rev_old)["turbines"]}
    missing = set(clean) - set(current)
    if missing:
        raise AvailabilityError(f"unknown unit_ids: {sorted(missing)[:10]}")
    rev = rev_old + 1
    now = _now()
    with dbm.session(db_path) as conn:
        for unit, t in current.items():
            new_rating = clean.get(unit, t["rated_mw"])
            conn.execute(
                "INSERT INTO turbines (site_id, unit_id, name, rated_mw, curve_id, config_revision, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (site_id, unit, t["name"], new_rating, t["curve_id"], rev, now),
            )
    return get_inventory(db_path, site_id, rev)


def delete_inventory(db_path, site_id: str) -> dict:
    """Remove the whole inventory via a NEW empty revision (audit-preserving)."""
    _load_site_tz(db_path, site_id)
    rev = current_inventory_revision(db_path, site_id) + 1
    now = _now()
    with dbm.session(db_path) as conn:
        marker = f"__empty__{rev}"
        conn.execute(
            "INSERT INTO turbines (site_id, unit_id, name, rated_mw, curve_id, config_revision, created_at_utc) "
            "VALUES (?, ?, NULL, 0.0001, NULL, ?, ?)",
            (site_id, marker, rev, now),
        )
    return get_inventory(db_path, site_id, rev)


def _load_site_tz(db_path, site_id: str):
    from .store import load_site

    return load_site(db_path, site_id)


# --- constraints --------------------------------------------------------------------


def normalize_constraint(row: dict, site: dict, known_units: set[str]) -> dict:
    """Validate and normalize one constraint row: kind, caps (MW and/or %,
    cross-checked consistent), unit list (must exist; farm-wide for
    export_cap), UTC times with explicit DST semantics."""
    kind = str(row.get("kind") or "").strip()
    if kind not in KINDS:
        raise AvailabilityError(f"kind must be one of {KINDS}")
    site_id = site["id"]

    unit_ids: list[str] | None = None
    raw_units = row.get("unit_ids")
    if kind == "export_cap":
        if raw_units:
            raise AvailabilityError("export_cap is farm-wide; unit_ids must be empty")
    else:
        if not isinstance(raw_units, list) or not raw_units:
            raise AvailabilityError(f"{kind} requires a non-empty unit_ids list")
        unit_ids = sorted({str(u).strip() for u in raw_units})
        missing = [u for u in unit_ids if u not in known_units]
        if missing:
            raise AvailabilityError(f"unknown unit_ids: {missing[:10]}")

    cap_mw = row.get("cap_mw")
    cap_percent = row.get("cap_percent")
    if kind == "offline":
        cap_mw = cap_percent = None
    elif kind == "curtailment":
        if cap_mw is None and cap_percent is None:
            raise AvailabilityError("curtailment requires cap_mw or cap_percent")
        rated_lookup = None
        if unit_ids and len(unit_ids) == 1:
            rated_lookup = None  # resolved after inventory fetch by caller
        if cap_mw is not None:
            cap_mw = _finite(cap_mw, "cap_mw", 0.0, 1e6)
        if cap_percent is not None:
            cap_percent = _finite(cap_percent, "cap_percent", 0.0, 100.0)
    else:  # export_cap
        if cap_mw is None:
            raise AvailabilityError("export_cap requires cap_mw")
        cap_mw = _finite(cap_mw, "cap_mw", 0.0, 1e6)
        cap_percent = None

    start_utc, warn = local_to_utc(str(row.get("start_local") or ""), site["timezone"])
    warnings = [warn] if warn else []
    end_local = row.get("end_local")
    if end_local in (None, ""):
        end_utc = None  # open-ended
    else:
        end_utc, w2 = local_to_utc(str(end_local), site["timezone"])
        if w2:
            warnings.append(w2)
    if end_utc is not None and end_utc <= start_utc:
        raise AvailabilityError("end must be after start (UTC)")

    return {
        "site_id": site_id,
        "kind": kind,
        "unit_ids": unit_ids,
        "cap_mw": cap_mw,
        "cap_percent": cap_percent,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "warnings": warnings,
    }


def current_constraints_revision(db_path, site_id: str) -> int:
    with dbm.session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM operational_constraints WHERE site_id=?",
            (site_id,),
        ).fetchone()
    return int(row["r"])


def get_constraints(db_path, site_id: str, revision: int | None = None) -> dict:
    rev = revision if revision is not None else current_constraints_revision(db_path, site_id)
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, kind, unit_ids_json, cap_mw, cap_percent, start_utc, end_utc, revision, "
            "created_at_utc, created_by, superseded_by FROM operational_constraints "
            "WHERE site_id=? AND revision=? AND superseded_by IS NULL ORDER BY id",
            (site_id, rev),
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["unit_ids"] = json.loads(item.pop("unit_ids_json")) if item["unit_ids_json"] else None
        if item["unit_ids"] == []:
            continue  # inert marker row (kept only to keep revisions monotonic)
        items.append(item)
    return {"site_id": site_id, "revision": rev, "constraints": items}


def add_constraints(db_path, site_id: str, rows: list[dict]) -> dict:
    """Add one or MANY constraints in ONE request (bulk edit never means one
    HTTP call per turbine). Creates a NEW revision: previous rows are copied
    forward unchanged; new rows are appended with `superseded_by=NULL`."""
    site = _load_site_tz(db_path, site_id)
    if not isinstance(rows, list) or not rows:
        raise AvailabilityError("rows must be a non-empty list")
    if len(rows) > 500:
        raise AvailabilityError("a single request may add at most 500 constraints")

    inv = get_inventory(db_path, site_id)
    known_units = {t["unit_id"] for t in inv["turbines"]}
    if not known_units:
        raise AvailabilityError("no turbine inventory for this site — generate it first")

    normalized = [normalize_constraint(r, site, known_units) for r in rows]
    # cross-check per-unit caps against per-unit ratings (over-nameplate rejected)
    unit_rated = {t["unit_id"]: t["rated_mw"] for t in inv["turbines"]}
    for n in normalized:
        if n["kind"] == "curtailment" and n["cap_mw"] is not None and n["unit_ids"]:
            for u in n["unit_ids"]:
                if n["cap_mw"] > unit_rated[u] + TOLERANCE:
                    raise AvailabilityError(f"cap_mw {n['cap_mw']} exceeds rated MW of {u}")
        if n["kind"] == "curtailment" and n["cap_percent"] is not None and n["unit_ids"]:
            for u in n["unit_ids"]:
                implied = n["cap_percent"] / 100.0 * unit_rated[u]
                if implied > unit_rated[u] + TOLERANCE:
                    raise AvailabilityError(f"cap_percent {n['cap_percent']}% exceeds rated MW of {u}")

    rev = current_constraints_revision(db_path, site_id) + 1
    now = _now()
    all_warnings: list[str] = []
    with dbm.session(db_path) as conn:
        old = conn.execute(
            "SELECT kind, unit_ids_json, cap_mw, cap_percent, start_utc, end_utc, created_by "
            "FROM operational_constraints WHERE site_id=? AND revision=? AND superseded_by IS NULL",
            (site_id, rev - 1) if rev > 1 else (site_id, rev),
        ).fetchall()
        for r in old:  # copy-forward current active set into the new revision
            conn.execute(
                "INSERT INTO operational_constraints (site_id, kind, unit_ids_json, cap_mw, cap_percent, "
                "start_utc, end_utc, revision, created_at_utc, created_by, superseded_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (site_id, r["kind"], r["unit_ids_json"], r["cap_mw"], r["cap_percent"], r["start_utc"], r["end_utc"], rev, now, r["created_by"]),
            )
        for n in normalized:
            conn.execute(
                "INSERT INTO operational_constraints (site_id, kind, unit_ids_json, cap_mw, cap_percent, "
                "start_utc, end_utc, revision, created_at_utc, created_by, superseded_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'owner', NULL)",
                (
                    site_id,
                    n["kind"],
                    dbm.j(n["unit_ids"]) if n["unit_ids"] else None,
                    n["cap_mw"],
                    n["cap_percent"],
                    n["start_utc"],
                    n["end_utc"],
                    rev,
                    now,
                ),
            )
            all_warnings.extend(n["warnings"])
    out = get_constraints(db_path, site_id, rev)
    out["warnings"] = all_warnings
    return out


def delete_constraints(db_path, site_id: str, ids: list[int]) -> dict:
    """Remove constraints by creating a NEW revision without them (auditable
    successor; the previous revision is preserved for reproducibility). When
    nothing remains, an inert marker row (empty unit list) keeps the revision
    monotonic so MAX(revision) can never regress to an older revision."""
    _load_site_tz(db_path, site_id)
    if not isinstance(ids, list) or not ids:
        raise AvailabilityError("ids must be a non-empty list")
    rev_old = current_constraints_revision(db_path, site_id)
    with dbm.session(db_path) as conn:
        old = conn.execute(
            "SELECT id, kind, unit_ids_json, cap_mw, cap_percent, start_utc, end_utc, created_by "
            "FROM operational_constraints WHERE site_id=? AND revision=? AND superseded_by IS NULL",
            (site_id, rev_old),
        ).fetchall()
        remove = set(int(i) for i in ids)
        found = {r["id"] for r in old}
        missing = remove - found
        if missing:
            raise AvailabilityError(f"unknown constraint ids in current revision: {sorted(missing)[:10]}")
        rev = rev_old + 1
        now = _now()
        kept = 0
        for r in old:
            if r["id"] in remove:
                continue
            kept += 1
            conn.execute(
                "INSERT INTO operational_constraints (site_id, kind, unit_ids_json, cap_mw, cap_percent, "
                "start_utc, end_utc, revision, created_at_utc, created_by, superseded_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (site_id, r["kind"], r["unit_ids_json"], r["cap_mw"], r["cap_percent"], r["start_utc"], r["end_utc"], rev, now, r["created_by"]),
            )
        if kept == 0:
            conn.execute(
                "INSERT INTO operational_constraints (site_id, kind, unit_ids_json, cap_mw, cap_percent, "
                "start_utc, end_utc, revision, created_at_utc, created_by, superseded_by) "
                "VALUES (?, 'offline', '[]', NULL, NULL, '1970-01-01T00:00:00Z', NULL, ?, ?, 'owner', NULL)",
                (site_id, rev, now),
            )
    return get_constraints(db_path, site_id, rev)


# --- scenario computation ------------------------------------------------------------


def _active_at(item: dict, t: str) -> bool:
    if t < item["start_utc"]:
        return False
    return item["end_utc"] is None or t < item["end_utc"]


def _most_restrictive_cap(constraints: list[dict], unit_id: str, t: str, rated_mw: float) -> float | None:
    """Most restrictive active per-unit MW cap at time t (offline handled
    separately and dominates)."""
    caps: list[float] = []
    for c in constraints:
        if c["kind"] != "curtailment" or not _active_at(c, t):
            continue
        ids = c["unit_ids"] or []
        if unit_id not in ids:
            continue
        if c["cap_mw"] is not None:
            caps.append(c["cap_mw"])
        elif c["cap_percent"] is not None:
            caps.append(c["cap_percent"] / 100.0 * rated_mw)
    return min(caps) if caps else None


def _is_offline(constraints: list[dict], unit_id: str, t: str) -> bool:
    return any(c["kind"] == "offline" and unit_id in (c["unit_ids"] or []) and _active_at(c, t) for c in constraints)


def _farm_export_cap(constraints: list[dict], t: str) -> float | None:
    caps = [c["cap_mw"] for c in constraints if c["kind"] == "export_cap" and _active_at(c, t) and c["cap_mw"] is not None]
    return min(caps) if caps else None


def _unit_curve(curve_by_id: dict, curve_id: str | None, site_curve_id: str) -> tuple:
    """Resolve a unit's curve: explicit override, else the site curve.
    Returns (points, mode) where mode is 'direct' | 'scaled' | None."""
    chosen = curve_id or site_curve_id
    curve = curve_by_id.get(chosen)
    if curve is None or curve["rated_power_mw"] is None:
        return None, None
    return curve, "direct"


def scenario_for_run(db_path, site_id: str, run_id: int, inventory_revision: int | None = None, constraints_revision: int | None = None) -> dict:
    """Deterministically regenerate the availability-adjusted scenario for one
    archived run from immutable inputs (forecast points + inventory + constraint
    revisions). Never touches stored runs, points or actuals."""
    from .store import curve_row_to_power_curve

    site = _load_site_tz(db_path, site_id)
    inv = get_inventory(db_path, site_id, inventory_revision)
    cons = get_constraints(db_path, site_id, constraints_revision)
    inv_rev, cons_rev = inv["revision"], cons["revision"]
    loss_factor = float(site.get("loss_factor") or 0.0)

    with dbm.session(db_path) as conn:
        curve_rows = conn.execute("SELECT * FROM power_curves").fetchall()
        curve_by_id = {r["id"]: dict(r) for r in curve_rows}
        run = conn.execute(
            "SELECT id, model_id, run_issued_utc, raw_json FROM forecast_runs WHERE id=? AND site_id=?",
            (run_id, site_id),
        ).fetchone()
        if run is None:
            raise KeyError(f"no run {run_id} for site {site_id}")
        points = conn.execute(
            "SELECT valid_at_utc, wind_speed_mps, modeled_power_mw, interval_energy_mwh, interval_hours "
            "FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc",
            (run_id,),
        ).fetchall()

    curve_mode_used = "direct"
    real_turbines = inv["turbines"]
    installed = inv["installed_mw"]
    allow_scaled = bool(int(site.get("allow_scaled_reference") or 0))
    # Baseline comparability: the archived baseline modeled_power_mw was computed
    # from the site configuration AT INGESTION. If the current inventory differs
    # (installed MW mismatch) or any unit needs a scaled/incompatible curve, the
    # archived baseline is NOT a valid "unconstrained" reference for loss
    # identities — the scenario-equivalent base is reported separately.
    asset_capacity = float(site.get("capacity_mw") or 0.0)
    baseline_comparable = True
    baseline_reasons: list[str] = []
    if abs(installed - asset_capacity) > 0.05:
        baseline_comparable = False
        baseline_reasons.append(
            f"current inventory installed {installed} MW differs from the asset capacity at ingestion ({asset_capacity} MW)"
        )

    rows_out: list[dict] = []
    for p in points:
        valid = p["valid_at_utc"]
        hours = float(p["interval_hours"] or 1.0)
        wind = p["wind_speed_mps"]
        base_mw = p["modeled_power_mw"]  # v1 baseline (site config at ingestion)

        if wind is None or not real_turbines:
            rows_out.append(
                {
                    "valid_at_utc": valid,
                    "base_mw": base_mw,
                    "adjusted_mw": None,
                    "pre_export_mw": None,
                    "scenario_base_mw": None,
                    "lost_to_unit_mwh": None,
                    "lost_to_export_mwh": None,
                    "interval_hours": hours,
                    "status": "missing-wind" if wind is None else "no-inventory",
                }
            )
            continue

        pre_export = 0.0
        base_sum = 0.0
        curve_ok = True
        curve_error: str | None = None
        for t in real_turbines:
            curve_row, _mode = _unit_curve(curve_by_id, t["curve_id"], site.get("power_curve_id") or "")
            if curve_row is None:
                curve_ok = False
                break
            pc = curve_row_to_power_curve(curve_row)
            raw = pc.power_mw_per_turbine(wind)
            if raw is None:
                curve_ok = False
                break
            rated_t = float(t["rated_mw"])
            curve_rated = float(pc.rated_power_mw or 0.0)
            if curve_rated <= 0:
                curve_ok = False
                curve_error = "missing-curve"
                break
            if abs(curve_rated - rated_t) / rated_t <= CURVE_MATCH_TOLERANCE:
                # physically compatible reference curve (narrow tolerance, either direction)
                base_i = raw * (1.0 - loss_factor)
            elif allow_scaled:
                # explicit opt-in: normalized shape × per-unit rating — REFERENCE/UNVERIFIED
                base_i = (raw / curve_rated) * rated_t * (1.0 - loss_factor)
                curve_mode_used = "scaled-reference"
            else:
                # rated mismatch in either direction without opt-in: never a
                # silent clip or pseudo-accurate reuse
                curve_ok = False
                curve_error = "incompatible-curve"
                break
            # The per-unit nameplate clamp is part of the machine, not an
            # operational restriction: loss accounting measures against the
            # rated-clamped sum so the identity base_mw − lost_unit −
            # lost_export == adjusted holds against the stored baseline.
            base_sum += min(base_i, rated_t)
            if _is_offline(cons["constraints"], t["unit_id"], valid):
                continue
            cap = _most_restrictive_cap(cons["constraints"], t["unit_id"], valid, rated_t)
            pre_export += min(base_i, cap if cap is not None else rated_t)

        if not curve_ok:
            rows_out.append(
                {
                    "valid_at_utc": valid,
                    "base_mw": base_mw,
                    "adjusted_mw": None,
                    "pre_export_mw": None,
                    "scenario_base_mw": None,
                    "lost_to_unit_mwh": None,
                    "lost_to_export_mwh": None,
                    "interval_hours": hours,
                    "status": curve_error or "missing-curve",
                }
            )
            continue

        export_cap = _farm_export_cap(cons["constraints"], valid)
        limit = export_cap if export_cap is not None else installed
        adjusted = min(pre_export, limit)
        scenario_base = min(base_sum, installed)
        lost_unit = max(scenario_base - pre_export, 0.0) * hours
        lost_export = max(pre_export - adjusted, 0.0) * hours
        rows_out.append(
            {
                "valid_at_utc": valid,
                "base_mw": base_mw,
                "scenario_base_mw": round(scenario_base, 6),
                "adjusted_mw": round(adjusted, 6),
                "pre_export_mw": round(pre_export, 6),
                "lost_to_unit_mwh": round(lost_unit, 6),
                "lost_to_export_mwh": round(lost_export, 6),
                "interval_hours": hours,
                "status": "ok",
            }
        )

    # coverage-aware summaries per available window (never padded, never
    # zero-filled): a window totals only COMPLETE hours with computed values;
    # any missing/invalid hour makes the full-window total null/not-comparable
    # and the available-hours subtotal is reported separately. Intervals are
    # counted when their valid_at lies inside the window (interval-based
    # policy, documented); a 10-day window is included and flagged
    # short-horizon when the archived run cannot reach it.
    spec = None
    try:
        from .registry import get_model

        spec = get_model(run["model_id"])
    except KeyError:
        spec = None
    model_max_h = spec.max_lead_hours if spec else None

    def window_mwh(window_hours: float) -> dict:
        n = len(rows_out)
        first = parse_utc(rows_out[0]["valid_at_utc"]) if n else None
        last = parse_utc(rows_out[-1]["valid_at_utc"]) if n else None
        end_t = (first + timedelta(hours=window_hours)) if first else None
        # interval policy: a point belongs to the window when its valid_at lies
        # inside [first, first + window); durations are the archived per-point
        # interval_hours, never an assumed constant spacing
        sel = [r for r in rows_out if end_t and parse_utc(r["valid_at_utc"]) < end_t]
        valid_rows = [r for r in sel if r["status"] == "ok" and r["adjusted_mw"] is not None]
        # hours of the window without a computed value: gaps (invalid/missing
        # wind/curve) AND hours beyond the archived horizon — both make the
        # window incomplete; the archived-span distinction is `short_horizon`
        missing = max(0, int(round(window_hours - len(valid_rows))))
        archived_span_h = ((last - first).total_seconds() / 3600.0 + float(rows_out[-1]["interval_hours"] or 1.0)) if n else 0.0
        expected_hours = min(window_hours, archived_span_h)
        coverage = round(len(valid_rows) / window_hours, 4) if window_hours else None
        complete = bool(sel) and missing == 0 and len(valid_rows) >= window_hours - 1e-6
        out: dict = {
            "window_hours": window_hours,
            "start_valid_utc": rows_out[0]["valid_at_utc"] if n else None,
            "end_valid_utc": rows_out[-1]["valid_at_utc"] if n else None,
            "expected_hours": round(expected_hours, 3),
            "valid_hours": len(valid_rows),
            "missing_hours": missing,
            "coverage": coverage,
            "complete": complete,
            "base_mwh": round(sum((r["base_mw"] or 0.0) * r["interval_hours"] for r in valid_rows), 3) if complete else None,
            "adjusted_mwh": round(sum((r["adjusted_mw"] or 0.0) * r["interval_hours"] for r in valid_rows), 3) if complete else None,
            "lost_unit_mwh": round(sum(r["lost_to_unit_mwh"] or 0.0 for r in valid_rows), 3) if complete else None,
            "lost_export_mwh": round(sum(r["lost_to_export_mwh"] or 0.0 for r in valid_rows), 3) if complete else None,
        }
        # available-hours subtotal: computed values over the covered hours only,
        # always present and clearly labelled as PARTIAL when incomplete
        out["available_hours_subtotal"] = {
            "hours": len(valid_rows),
            "base_mwh": round(sum((r["base_mw"] or 0.0) * r["interval_hours"] for r in valid_rows), 3),
            "adjusted_mwh": round(sum((r["adjusted_mw"] or 0.0) * r["interval_hours"] for r in valid_rows), 3),
            "lost_unit_mwh": round(sum(r["lost_to_unit_mwh"] or 0.0 for r in valid_rows), 3),
            "lost_export_mwh": round(sum(r["lost_to_export_mwh"] or 0.0 for r in valid_rows), 3),
            "note": "subtotal over computed hours only; NOT comparable to a full-window total",
        }
        if n and archived_span_h < window_hours - 1e-6:
            out["short_horizon"] = True
        if model_max_h is not None and model_max_h < window_hours - 1e-6:
            out["short_horizon"] = True
        return out

    summary = {
        "installed_mw": installed,
        "curve_mode": curve_mode_used,
        "windows": {"24h": window_mwh(24), "7d": window_mwh(24 * 7), "10d": window_mwh(24 * 10)},
    }

    if curve_mode_used != "direct" and baseline_comparable:
        baseline_comparable = False
        baseline_reasons.append(f"curve mode is '{curve_mode_used}' — scenario base is not the archived site configuration")
    if not baseline_comparable:
        baseline_reasons.append(
            "loss identities use scenario_base_mw (the scenario-equivalent unconstrained base), "
            "not the archived baseline"
        )

    return {
        "site_id": site_id,
        "run_id": run_id,
        "model_id": run["model_id"],
        "run_issued_utc": run["run_issued_utc"],
        "inventory_revision": inv_rev,
        "constraints_revision": cons_rev,
        "algorithm_version": ALGORITHM_VERSION,
        "installed_mw": installed,
        "n_turbines": len(real_turbines),
        "points": rows_out,
        "summary": summary,
        "baseline_comparable": baseline_comparable,
        "baseline_comparable_reason": "; ".join(baseline_reasons) if baseline_reasons else None,
        "note": (
            "Availability-adjusted scenario: derived from the immutable archived run under the "
            "pinned inventory/constraint revisions (append-only). The 50% cap means 50% of rated "
            "MW as a maximum, not a multiplier of instantaneous output. Losses applied exactly "
            "once; window totals are coverage-aware and null when the window is incomplete."
        ),
    }


def site_status(db_path, site_id: str) -> dict:
    """Current operating picture: counts by status and the maximum available MW
    at rated wind under the ACTIVE constraints (no export cap beyond installed)."""
    inv = get_inventory(db_path, site_id)
    cons = get_constraints(db_path, site_id)
    turbines = [t for t in inv["turbines"] if not t["unit_id"].startswith("__empty__")]
    now = iso_z(datetime.now(timezone.utc))
    offline = 0
    curtailed = 0
    available_max = 0.0
    for t in turbines:
        if _is_offline(cons["constraints"], t["unit_id"], now):
            offline += 1
            continue
        cap = _most_restrictive_cap(cons["constraints"], t["unit_id"], now, t["rated_mw"])
        if cap is not None:
            curtailed += 1
            available_max += min(cap, t["rated_mw"])
        else:
            available_max += t["rated_mw"]
    export_cap = _farm_export_cap(cons["constraints"], now)
    return {
        "site_id": site_id,
        "installed_mw": inv["installed_mw"],
        "available_max_mw": round(available_max, 3),
        "offline_count": offline,
        "curtailed_count": curtailed,
        "farm_export_cap_mw": export_cap,
        "n_turbines": len(turbines),
        "inventory_revision": inv["revision"],
        "constraints_revision": cons["revision"],
    }
