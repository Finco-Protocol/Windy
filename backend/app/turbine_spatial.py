"""Optional 26-coordinate turbine model, separate from immutable one-spot archive.

The numerical inputs are per-coordinate Open-Meteo *modelled* wind, not anemometer
measurements or a certified CFD/wake-loss simulation. Only an explicit POST
fetches new spatial weather. GETs are read-only; snapshots are append-only.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from . import availability as av
from .height import resolve_hub_speed
from .openmeteo import OMError
from .registry import WINDY_MODELS
from .store import curve_row_to_power_curve, load_site
from .siteconfig import get_locations, load_owner_locations_package
from .horizon_policy import select_forecast_run

MAX_TURBINES = 26  # explicitly bounded Krnovo pilot
MAX_MODEL_REQUESTS = len(WINDY_MODELS)


def _utc(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _coords(db_path, site_id: str) -> tuple[dict, list[dict], dict]:
    site = load_site(db_path, site_id)
    inv = av.get_inventory(db_path, site_id)
    turbines = inv["turbines"]
    virtual = False
    if not turbines and site_id == "krnovo" and site["turbine_count"] == 26:
        # Read-only preliminary inventory; no revisions, real constraints or
        # configuration records are changed by this projection.
        turbines = [
            {"unit_id": f"WTG-{i:03d}", "rated_mw": site["turbine_rated_mw"], "curve_id": None}
            for i in range(1, 27)
        ]
        virtual = True
    if len(turbines) != 26 or len(turbines) > MAX_TURBINES:
        raise ValueError("Per turbine requires all 26 units; inventory incomplete or different turbine count")
    known = {t["unit_id"] for t in turbines}
    if len(known) != 26:
        raise ValueError("Duplicate unit ID in turbine inventory")
    stored = get_locations(db_path, site_id)
    if stored["n_locations"] == 26 and {x["unit_id"] for x in stored["locations"]} == known:
        loc = stored["locations"]
        source = "db-owner-location-revision"
        revision = stored["revision"]
        provisional = any("provisional" in x.get("source", "").lower() for x in loc)
    elif site_id == "krnovo":
        pkg = load_owner_locations_package()
        loc = pkg["records"]
        if len(loc) != 26 or {x["unit_id"] for x in loc} != known:
            raise ValueError("26 owner GPS positions cannot be matched to active WTG IDs; confirm mapping")
        source = "owner-file-provisional"
        revision = 0
        provisional = True
    else:
        raise ValueError("Missing 26 matching turbine GPS locations (no one-spot substitution)")
    by_unit = {t["unit_id"]: t for t in turbines}
    locations = [{**x, "rated_mw": by_unit[x["unit_id"]]["rated_mw"],
                  "curve_id": by_unit[x["unit_id"]]["curve_id"]} for x in sorted(loc, key=lambda r:r["unit_id"])]
    fingerprint = hashlib.sha256(json.dumps(
        [[x["unit_id"], x["latitude"], x["longitude"], site["hub_height_m"]] for x in locations],
        separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    meta = {"location_source": source, "location_revision": revision,
            "location_fingerprint": fingerprint, "provisional_mapping": provisional,
            "virtual_inventory": virtual, "inventory_revision": inv["revision"],
            "n_turbines": len(locations)}
    return site, locations, meta


def _parse_multi(model, response: list[dict], locations: list[dict], hub_height_m: float) -> tuple[dict, int]:
    if len(response) != len(locations):
        raise OMError("Upstream returned a different number of locations")
    by_unit: dict = {}
    grid = set()
    for loc, raw in zip(locations, response):
        hourly = raw.get("hourly")
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            raise OMError(f"Missing hourly time series for {loc['unit_id']}")
        dates = hourly["time"]
        if len(dates) != len(set(dates)) or dates != sorted(dates):
            raise OMError(f"Non-monotonic or duplicated timestamps for {loc['unit_id']}")
        values = {}
        for height in model.speed_heights:
            arr = hourly.get(f"wind_speed_{height}m")
            if arr is None:
                arr = hourly.get(f"wind_speed_{height}m_{model.id}")
            if not isinstance(arr, list) or len(arr) != len(dates):
                arr = [None] * len(dates)
            values[height] = arr
        series = []
        for i, stamp in enumerate(dates):
            speed_levels = {float(h): values[h][i] for h in model.speed_heights}
            try:
                wind, status = resolve_hub_speed(speed_levels, hub_height_m, model.height_method)
            except (ValueError, TypeError, OverflowError):
                wind, status = None, "missing-height"
            if wind is not None and (not math.isfinite(float(wind)) or wind < 0):
                wind, status = None, "invalid-wind"
            series.append([_utc(stamp).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           round(float(wind), 5) if wind is not None else None, status])
        by_unit[loc["unit_id"]] = {
            "latitude": float(loc["latitude"]), "longitude": float(loc["longitude"]),
            "elevation_m": loc.get("elevation_m"), "series": series,
        }
        if raw.get("latitude") is not None and raw.get("longitude") is not None:
            grid.add((round(float(raw["latitude"]), 3), round(float(raw["longitude"]), 3)))
    return by_unit, len(grid)


def collect_per_turbine(db_path, client, site_id: str, days: int = 16) -> dict:
    """Bounded explicit collection: one 26-coordinate request per provider.

    Each snapshot is linked to the exact chosen spot issue (provenance only).
    No new issues are invented and central archive points never change.
    """
    site, locations, meta = _coords(db_path, site_id)
    reports: list[dict] = []
    with dbm.session(db_path) as conn:
        selected = [(spec, select_forecast_run(conn, site_id, spec)[0]) for spec in WINDY_MODELS]
    for spec, run in selected:
        info = {"model_id": spec.id, "status": "UNAVAILABLE", "n_turbines": len(locations)}
        if run is None:
            info["reason"] = "No archived issue; run the spot collector first"
            reports.append(info)
            continue
        if not run["run_issued_utc"]:
            info["reason"] = "Unknown-issue live fallback cannot be matched to a per-coordinate issue"
            reports.append(info)
            continue
        with dbm.session(db_path) as conn:
            exists = conn.execute(
                "SELECT id, unique_grid_cells FROM turbine_forecast_snapshots "
                "WHERE run_id=? AND locations_fingerprint=? AND inventory_revision=?",
                (run["id"], meta["location_fingerprint"], meta["inventory_revision"]),
            ).fetchone()
        if exists:
            reports.append({**info, "status": "ALREADY_ARCHIVED",
                            "snapshot_id": exists["id"], "unique_grid_cells": exists["unique_grid_cells"]})
            continue
        try:
            raw = client.fetch_multi_single_run(
                locations, spec, run["run_issued_utc"], min(days, spec.request_days),
            )
            parsed, grid_count = _parse_multi(spec, raw, locations, float(site["hub_height_m"]))
            # Snapshot is immutable, as is the linked central issue.
            with dbm.session(db_path) as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO turbine_forecast_snapshots "
                    "(run_id,site_id,model_id,locations_fingerprint,location_revision,location_source,"
                    "inventory_revision,collected_at_utc,unit_count,unique_grid_cells,payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run["id"], site_id, spec.id, meta["location_fingerprint"], meta["location_revision"],
                     meta["location_source"], meta["inventory_revision"],
                     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     26, grid_count, dbm.j(parsed)),
                )
            reports.append({**info, "status": "OK", "snapshot_id": cur.lastrowid,
                            "unique_grid_cells": grid_count})
        except (OMError, ValueError, TypeError, KeyError) as exc:
            reports.append({**info, "status": "UNAVAILABLE", "reason": str(exc)[:250]})
    return {"site_id": site_id, "mode": "per_turbine",
            "n_turbines": 26, "locations": meta, "models": reports,
            "note": "Provisional WTG mapping; raw NWP wind, not onsite measurements; no wake/CFD simulation"}


def _power(wind: float | None, turbine: dict, curve_by_id: dict, site: dict) -> tuple[float | None, str]:
    if wind is None:
        return None, "missing-wind"
    chosen = turbine.get("curve_id") or site.get("power_curve_id")
    curve_row = curve_by_id.get(chosen)
    if curve_row is None:
        return None, "missing-curve"
    curve = curve_row_to_power_curve(curve_row)
    rated = float(turbine["rated_mw"])
    if curve.rated_power_mw <= 0:
        return None, "missing-curve"
    raw = curve.power_mw_per_turbine(wind)
    if raw is None:
        return None, "missing-wind"
    if abs(curve.rated_power_mw - rated) / rated <= av.CURVE_MATCH_TOLERANCE:
        base = raw
    elif bool(int(site.get("allow_scaled_reference") or 0)):
        base = raw * rated / curve.rated_power_mw
    else:
        return None, "incompatible-curve"
    return round(min(max(base * (1 - float(site["loss_factor"])), 0), rated), 6), "ok"


def read_per_turbine(db_path, site_id: str, days: int = 7) -> dict:
    """Read-only farm/day and 26-turbine/day summaries for all providers.

    A farm-hour is valid ONLY if every unit has wind + compatible curve.
    Missing turbines produce a partial day, never an estimated zero or a
    disguised sum of 25/26 machines.
    """
    if not 1 <= days <= 15:
        raise ValueError("days must be 1..15")
    site, locations, meta = _coords(db_path, site_id)
    by_location = {x["unit_id"]: x for x in locations}
    constraints = av.get_constraints(db_path, site_id)["constraints"]
    tz = ZoneInfo(site["timezone"])
    with dbm.session(db_path) as conn:
        curve_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM power_curves")}
        selected = [(spec, select_forecast_run(conn, site_id, spec)[0]) for spec in WINDY_MODELS]
        snapshots = {}
        for spec, run in selected:
            if run is None:
                continue
            snapshots[spec.id] = conn.execute(
                "SELECT * FROM turbine_forecast_snapshots WHERE run_id=? "
                "AND locations_fingerprint=? AND inventory_revision=?",
                (run["id"], meta["location_fingerprint"], meta["inventory_revision"]),
            ).fetchone()
    # Day window is the SITE calendar, independent of the run initialization.
    today = datetime.now(timezone.utc).astimezone(tz).date()
    from datetime import timedelta
    dates = [(today + timedelta(days=i)).isoformat() for i in range(days)]
    models = []
    for spec, run in selected:
        snap = snapshots.get(spec.id)
        item: dict = {"model_id": spec.id, "label": spec.label, "met_only": spec.met_only,
                      "status": "NO_RUN" if run is None else "NO_SPATIAL_SNAPSHOT",
                      "issue_utc": run["run_issued_utc"] if run is not None else None,
                      "grid_cells": None, "daily": [], "turbines": [], "hourly": [],
                      "warning": None}
        if snap is None:
            item["warning"] = "Odaberite Osvježi per turbine; nema valjanih podataka po koordinatama."
            models.append(item)
            continue
        unit_winds = json.loads(snap["payload_json"])
        if spec.met_only:
            met_times: dict[str, list[float]] = {}
            for unit_data in unit_winds.values():
                for stamp, wind, _status in unit_data["series"]:
                    if wind is not None:
                        met_times.setdefault(stamp, []).append(float(wind))
            item["hourly"] = [
                {"valid_at_utc": stamp, "wind_speed_mps": round(sum(winds)/26, 5) if len(winds)==26 else None,
                 "modeled_power_mw": None, "adjusted_mw": None,
                 "interval_energy_mwh": None, "interval_hours": 1., "status": "met-only"}
                for stamp, winds in sorted(met_times.items())
                if _utc(stamp).astimezone(tz).date().isoformat() in dates
            ]
            item["status"] = "MET_ONLY"
            item["grid_cells"] = snap["unique_grid_cells"]
            item["warning"] = "26 koordinata, samo 10 m meteorološki vjetar; bez modeliranja MWh."
            models.append(item)
            continue
        if set(unit_winds) != set(by_location):
            item["status"] = "INVALID_SNAPSHOT"
            item["warning"] = "Lokacije i WTG oznake se ne podudaraju; bez zamjene 1-spot vrijednostima."
            models.append(item)
            continue
        by_t = {t: {} for t in dates}
        per_unit = {}
        for unit, u in unit_winds.items():
            series = {a[0]: a[1] for a in u["series"]}
            per_unit[unit] = series
            for stamp in series:
                d = _utc(stamp).astimezone(tz).date().isoformat()
                if d in by_t:
                    by_t[d][stamp] = True
        # The actual model may have 3-hour resolution: derive intervals from
        # the reference archived run, never assume missing hours equal zero.
        with dbm.session(db_path) as conn:
            intervals = {r["valid_at_utc"]: float(r["interval_hours"]) for r in conn.execute(
                "SELECT valid_at_utc,interval_hours FROM forecast_points WHERE run_id=?", (run["id"],)
            ).fetchall()}
        all_times = sorted(set.union(*[set(x) for x in per_unit.values()])) if per_unit else []
        unit_daily = {u: {d: {"mwh": 0., "adjusted_mwh": 0., "hours": 0., "wind_sum": 0., "wind_n": 0}
                          for d in dates} for u in per_unit}
        hourly: list[dict] = []
        curve_failures = 0
        daily = {d: {"date": d, "base_mwh": 0., "adjusted_mwh": 0.,
                     "hours_counted": 0., "missing_intervals": 0,
                     "n_turbines": 26} for d in dates}
        for stamp in all_times:
            d = _utc(stamp).astimezone(tz).date().isoformat()
            if d not in daily:
                continue
            hours = intervals.get(stamp, 1.)
            if not 0 < hours <= 6:
                continue
            base_sum, adjusted_sum, valid_count = 0., 0., 0
            wind_sum, wind_count = 0., 0
            powers = []
            for unit, series in per_unit.items():
                wind = series.get(stamp)
                base, status = _power(wind, by_location[unit], curve_by_id, site)
                if wind is not None:
                    wind_sum += float(wind)
                    wind_count += 1
                    ud = unit_daily[unit][d]
                    ud["wind_sum"] += wind
                    ud["wind_n"] += 1
                if base is None and status in ("incompatible-curve", "missing-curve"):
                    curve_failures += 1
                if base is not None:
                    valid_count += 1
                    ud = unit_daily[unit][d]
                    ud["mwh"] += base * hours
                    ud["hours"] += hours
                    if av._is_offline(constraints, unit, stamp):
                        adjusted = 0.
                    else:
                        cap = av._most_restrictive_cap(constraints, unit, stamp, float(by_location[unit]["rated_mw"]))
                        adjusted = min(base, cap) if cap is not None else base
                    ud["adjusted_mwh"] += adjusted * hours
                    powers.append((unit, base, adjusted))
                    base_sum += base
                    adjusted_sum += adjusted
            if valid_count != 26:
                daily[d]["missing_intervals"] += 1
                hourly.append({"valid_at_utc": stamp,
                               "wind_speed_mps": round(wind_sum / 26, 5) if wind_count == 26 else None,
                               "modeled_power_mw": None, "adjusted_mw": None,
                               "interval_energy_mwh": None, "interval_hours": hours,
                               "status": "missing-unit-or-curve"})
                continue
            limit = av._farm_export_cap(constraints, stamp)
            adjusted_sum = min(adjusted_sum, limit) if limit is not None else adjusted_sum
            daily[d]["base_mwh"] += base_sum * hours
            daily[d]["adjusted_mwh"] += adjusted_sum * hours
            daily[d]["hours_counted"] += hours
            hourly.append({"valid_at_utc": stamp,
                           "wind_speed_mps": round(wind_sum / 26, 5) if wind_count == 26 else None,
                           "modeled_power_mw": round(base_sum, 6), "adjusted_mw": round(adjusted_sum, 6),
                           "interval_energy_mwh": round(base_sum * hours, 6),
                           "interval_hours": hours, "status": "ok"})
        item["status"] = "OK" if any(r["hours_counted"] > 0 for r in daily.values()) else (
            "NO_VALID_POWER" if curve_failures else "NO_COMPLETE_WTG_HOURS"
        )
        item["hourly"] = hourly
        item["grid_cells"] = snap["unique_grid_cells"]
        item["location_source"] = snap["location_source"]
        item["warning"] = ("Provider je vratio jednu NWP mrežnu ćeliju za sve lokacije; "
                           "26 koordinata ne znači 26 neovisnih prognoza."
                           if snap["unique_grid_cells"] == 1 else
                           "Modelirani vjetar po koordinati; broj odvojenih NWP ćelija nije potvrđen."
                           if snap["unique_grid_cells"] == 0 else
                           "Modelirani vjetar po koordinati; bez wake/CFD korekcije.")
        if curve_failures:
            item["warning"] += (" Neki ili svi WTG imaju nekompatibilnu referentnu krivulju; "
                                "provjerite nazivnu snagu i eksplicitni scaled-reference opt-in.")
        item["daily"] = [
            {**d, "base_mwh": round(d["base_mwh"], 3) if d["hours_counted"] else None,
             "adjusted_mwh": round(d["adjusted_mwh"], 3) if d["hours_counted"] else None,
             "partial": d["missing_intervals"] > 0 or d["hours_counted"] < 23}
            for d in daily.values()
        ]
        item["turbines"] = [
            {"unit_id": unit, "latitude": by_location[unit]["latitude"],
             "longitude": by_location[unit]["longitude"], "elevation_m": by_location[unit].get("elevation_m"),
             "daily": [
                 {"date": d, "base_mwh": round(row["mwh"], 3) if row["hours"] else None,
                  "adjusted_mwh_pre_export_cap": round(row["adjusted_mwh"], 3) if row["hours"] else None,
                  "wind_mean_mps": round(row["wind_sum"]/row["wind_n"], 3) if row["wind_n"] else None,
                  "hours": round(row["hours"], 3)}
                 for d, row in per_unit_days.items()
             ]}
            for unit, per_unit_days in sorted(unit_daily.items())
        ]
        models.append(item)
    return {
        "site_id": site_id, "mode": "per_turbine", "days": days,
        "location_info": meta, "models": models,
        "note": "26 lokacija (privremeno mapiranje prema vlasničkom XLSX). "
                "NWP grid interpolacija; nije stvarno mjerenje, wake/terrain CFD nisu modelirani. "
                "Prilagođena farm-suma uključuje zadana per-unit ograničenja i farm export cap. "
                "Neuspjela lokacija ne pretvara se u 0 MW."
    }

 

def build_per_turbine_xlsx(spatial: dict, unit: str = "mwh", mode: str = "baseline") -> bytes:
    """Read-only export of the same verified 26-coordinate model shown on screen.

    This workbook NEVER silently exports the one-spot model under a spatial
    name. No formulas: guard even metadata strings against formula injection.
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    if unit not in ("mwh", "gwh") or mode not in ("baseline", "adjusted"):
        raise ValueError("unit must be mwh/gwh, mode baseline/adjusted")
    divisor = 1000 if unit == "gwh" else 1
    valkey = "adjusted_mwh" if mode == "adjusted" else "base_mwh"
    wsname = f"Farm {unit.upper()}"
    wb = Workbook()
    farm = wb.active
    farm.title = wsname
    farm.append(["Datum (site lokalno)", "Model", f"Farm {unit.upper()}",
                 "Sati potpune pokrivenosti", "N turbina", "Djelomično", "Status"])
    units = wb.create_sheet(f"WTG {unit.upper()}")
    units.append(["Datum", "Model", "Turbina", f"Base {unit.upper()}",
                  f"Adjusted pre-export {unit.upper()}", "Vjetar m/s",
                  "Sati", "Lat", "Lon", "Nadmorska visina m"])
    coverage = wb.create_sheet("Pokrivenost")
    coverage.append(["Model", "Issue UTC", "Status", "NWP grid cells", "Izvor lokacija", "Napomena"])
    for m in spatial["models"]:
        def cell(s):
            if not isinstance(s, str):
                return s
            return "'" + s if s[:1] in ("=", "+", "-", "@") else s

        coverage.append([cell(m["label"]), cell(m["issue_utc"]), cell(m["status"]),
                         m["grid_cells"], cell(m.get("location_source") or ""),
                         cell(m.get("warning") or "")])
        for d in m["daily"]:
            val = d[valkey]
            farm.append([d["date"], m["label"], (val / divisor if val is not None else None),
                         d["hours_counted"], d["n_turbines"], d["partial"], m["status"]])
        for t in m["turbines"]:
            for d in t["daily"]:
                base = d["base_mwh"]
                adj = d["adjusted_mwh_pre_export_cap"]
                units.append([d["date"], m["label"], t["unit_id"],
                              base / divisor if base is not None else None,
                              adj / divisor if adj is not None else None,
                              d["wind_mean_mps"], d["hours"],
                              t["latitude"], t["longitude"], t["elevation_m"]])
    meta = wb.create_sheet("Metapodaci")
    for row in [
        ("Mod", "per_turbine"), ("N turbina", 26),
        ("Model", "Independent NWP coordinates; NOT 26 anemometer measurements"),
        ("Izvor i ID mapping", spatial["location_info"]["location_source"]),
        ("Privremeno mapiranje", str(spatial["location_info"]["provisional_mapping"])),
        ("Izvoz", mode), ("Jedinica", unit.upper()),
        ("Farm export cap", "Applied ONLY to adjusted farm total; WTG adjusted values are PRE export cap"),
        ("Ograničenja", "Per-unit OFF and MW/% caps applied by UTC hour"),
        ("Ograničenja modela", "No wake/CFD or unverified topographic uplift"),
    ]:
        meta.append(row)
    for sheet in wb:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="203850")
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
