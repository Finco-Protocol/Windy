"""Correction E Addendum D — read-only Excel export (7/10/15 days).

One workbook, five sheets: Dnevna prognoza (daily per model + Prosjek +
UKUPNO over EXACTLY the displayed rows), Satni vjetar (all models incl. UKMO
10 m), Satna proizvodnja (production models, MW, baseline-or-adjusted), Pokrivenost
(per-model provenance/status) and Metapodaci.

Invariants: strictly read-only (SELECTs only, no provider calls); numbers
stay raw MWh internally and convert to the display unit only at cell write;
missing ≠ 0 (blank cell + Pokrivenost reason); UKMO never gets an energy
value; adjusted mode never falls back to baseline; formula-injection safe.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .registry import WINDY_MODELS

MAX_EXPORT_DAYS = 15
HEADER_FILL = PatternFill("solid", fgColor="1a2638")
HEADER_FONT = Font(color="F4ECD8", bold=True)
TOTAL_FONT = Font(bold=True)


def _safe_str(value: object) -> str:
    """Formula-injection guard: Excel evaluates strings starting with = + - @."""
    s = str(value) if value is not None else ""
    if s[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_span_utc(tz: ZoneInfo, local_date: str) -> tuple[datetime, datetime]:
    d = datetime.strptime(local_date, "%Y-%m-%d")

    def midnight(day: datetime) -> datetime:
        return day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)

    return midnight(d).astimezone(timezone.utc), midnight(d + timedelta(days=1)).astimezone(timezone.utc)


def _daily_energy(points: list[dict], tz: ZoneInfo, value_key: str) -> list[dict]:
    """Allocate interval energy values across site-local days; union coverage
    per day; an interval with a null value contributes MISSING hours, never 0."""
    daily: dict[str, dict] = {}
    for p in points:
        st = _parse_utc(p["valid_at_utc"])
        dur = float(p.get("interval_hours") or 1.0)
        value = p.get(value_key)
        remaining, cursor = dur, st
        while remaining > 1e-9:
            day_key = cursor.astimezone(tz).strftime("%Y-%m-%d")
            d_start, d_end = _day_span_utc(tz, day_key)
            seg_end = min(cursor + timedelta(hours=remaining), d_end)
            overlap = (min(seg_end, d_end) - max(cursor, d_start)).total_seconds() / 3600.0
            if overlap <= 0:
                cursor = d_end
                continue
            slot = daily.setdefault(day_key, {"mwh": 0.0, "segs": []})
            if value is not None:
                slot["mwh"] += value * (overlap / dur)
                slot["segs"].append((max(cursor, d_start), min(seg_end, d_end)))
            remaining -= overlap
            cursor = min(seg_end, d_end) if seg_end > cursor else d_end
    out = []
    for k in sorted(daily):
        slot = daily[k]
        merged: list[tuple[datetime, datetime]] = []
        for s, e in sorted(slot["segs"]):
            if merged and s <= merged[-1][1] + timedelta(seconds=1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        d_start, d_end = _day_span_utc(tz, k)
        expected = (d_end - d_start).total_seconds() / 3600.0
        union = sum((e - s).total_seconds() / 3600.0 for s, e in merged)
        out.append(
            {
                "date": k,
                "mwh": round(slot["mwh"], 4) if slot["segs"] else None,
                "hours_counted": round(union, 3),
                "hours_expected": round(expected, 3),
                "hours_missing": round(max(expected - union, 0.0), 3),
                "partial": union < expected - 1e-6,
                "has_data": bool(slot["segs"]),
            }
        )
    return out


def _hourly_aligned_daily_avg(mw_points: dict[str, list[dict]], tz: ZoneInfo, avg_models: list[str]) -> list[dict]:
    """Hourly-aligned ensemble average (UI semantics): for every elementary
    UTC segment covered WHOLLY by a model's interval, average the contributing
    models' MW; energy = avgMW × segment hours, allocated to local days."""
    boundaries: set[datetime] = set()
    series: dict[str, list[tuple[datetime, datetime, float]]] = {}
    for mid in avg_models:
        ivs = []
        for p in mw_points.get(mid, []):
            mw = p.get("mw_for_avg")
            if mw is None:
                continue
            s = _parse_utc(p["valid_at_utc"])
            e = s + timedelta(hours=float(p.get("interval_hours") or 1.0))
            ivs.append((s, e, mw))
            boundaries.add(s)
            boundaries.add(e)
        if ivs:
            series[mid] = ivs
    if not series or not boundaries:
        return []
    bts = sorted(boundaries)
    daily: dict[str, float] = {}
    n_min: dict[str, int] = {}
    n_max: dict[str, int] = {}
    covered: dict[str, list[tuple[datetime, datetime]]] = {}
    for i in range(len(bts) - 1):
        s, e = bts[i], bts[i + 1]
        contributors = [mw for mid in series for (s0, e0, mw) in series[mid] if s0 <= s and e <= e0]
        if not contributors:
            continue
        avg_mw = sum(contributors) / len(contributors)
        cur = s
        while cur < e - timedelta(seconds=1):
            day_key = cur.astimezone(tz).strftime("%Y-%m-%d")
            d_start, d_end = _day_span_utc(tz, day_key)
            seg_end = min(e, d_end)
            hours = (seg_end - cur).total_seconds() / 3600.0
            if hours > 0:
                daily[day_key] = daily.get(day_key, 0.0) + avg_mw * hours
                covered.setdefault(day_key, []).append((cur, seg_end))
                # Correction F P1-9: N min/max tracks PER-HOUR contributor
                # counts (staggered coverage: A 00-12h + B 12-24h => 1..2)
                n_min[day_key] = min(n_min.get(day_key, len(contributors)), len(contributors))
                n_max[day_key] = max(n_max.get(day_key, len(contributors)), len(contributors))
            cur = seg_end if seg_end > cur else e
    out = []
    for k in sorted(daily):
        merged: list[tuple[datetime, datetime]] = []
        for s, e in sorted(covered.get(k, [])):
            if merged and s <= merged[-1][1] + timedelta(seconds=1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        out.append({"date": k, "mwh": round(daily[k], 4), "hours_counted": round(sum((e - s).total_seconds() / 3600.0 for s, e in merged), 3), "n_min": n_min.get(k, 0), "n_max": n_max.get(k, 0), "has_data": True})
    return out


def _sanitize_sheet_name(name: str) -> str:
    for ch in "[]:*?/\\":
        name = name.replace(ch, "-")
    return name[:31]


def build_export_xlsx(db_path, site: dict, days: int, mode: str, unit: str, avg_models: list[str], generated_at_utc: str, avg_enabled: bool = True) -> tuple[bytes, dict]:
    """Build the workbook bytes. Read-only: SELECTs + scenario_for_run reads.
    avg_enabled=False (Correction F P1-10) leaves Prosjek columns blank and
    the metadata records that the UI toggle was OFF for this export."""
    from .availability import scenario_for_run

    days = min(max(int(days), 1), MAX_EXPORT_DAYS)
    unit = "gwh" if unit == "gwh" else "mwh"
    mode = "adjusted" if mode == "adjusted" else "baseline"
    tz = ZoneInfo(site["timezone"])
    site_id = site["id"]
    factor = 1.0 / 1000.0 if unit == "gwh" else 1.0  # display-only conversion at the END

    per_model: dict[str, dict] = {}
    for spec in WINDY_MODELS:
        run = dbm_run(db_path, site_id, spec.id)
        info: dict = {
            "model_id": spec.id,
            "label": spec.label,
            "provider": spec.provider,
            "met_only": spec.met_only,
            "speed_heights": list(spec.speed_heights),
            "height_method": spec.height_method,
            "request_days": spec.request_days,
            "max_lead_hours": spec.max_lead_hours,
            "status": "NO_RUN",
            "reason": "nema arhiviranog run-a",
            "run_issued_utc": None,
            "fetched_at_utc": None,
            "points": [],
        }
        if run is not None:
            pts = dbm_points(db_path, run["id"])
            nn = sum(1 for p in pts if p["wind_speed_mps"] is not None)
            statuses: dict[str, int] = {}
            for p in pts:
                statuses[p["status"]] = statuses.get(p["status"], 0) + 1
            if not pts:
                info["status"], info["reason"] = "NO_RUN", "run bez tocaka"
            elif nn == 0:
                info["status"], info["reason"] = "MISSING_HEIGHT", f"statusi: {statuses}"
            else:
                info["status"] = "OK" if nn == len(pts) else "PARTIAL"
                info["reason"] = None if nn == len(pts) else f"statusi: {statuses}"
            info.update({"run_issued_utc": run["run_issued_utc"] or "ISSUE_UNKNOWN", "fetched_at_utc": run["fetched_at_utc"], "points": pts, "n_points": len(pts), "n_wind_valid": nn})
        if mode == "adjusted" and not spec.met_only:
            if run is None:
                info["status"], info["reason"] = "NO_RUN", "nema run-a za adjusted scenario"
                info["adjusted"] = None
            else:
                try:
                    sc = scenario_for_run(db_path, site_id, run["id"])
                    usable = bool(sc["points"]) and any(p.get("adjusted_mw") is not None for p in sc["points"])
                    info["adjusted"] = sc if usable else None
                    if not usable:
                        dom = sorted({p["status"] for p in sc["points"]})
                        info["status"] = "INCOMPATIBLE_CURVE" if dom and set(dom) <= {"incompatible-curve", "missing-curve"} else "ADJUSTED_UNAVAILABLE"
                        info["reason"] = f"adjusted nedostupan: {', '.join(dom) or 'bez vrijednosti'}"
                except Exception as exc:
                    info["adjusted"] = None
                    info["status"] = "ADJUSTED_UNAVAILABLE"
                    info["reason"] = f"adjusted greska: {exc}"
        per_model[spec.id] = info

    # ---- daily rows per production model (baseline or adjusted) ----
    daily_map: dict[str, dict[str, float | None]] = {}
    mw_points: dict[str, list[dict]] = {}
    hourly_prod_points: dict[str, list[dict]] = {}
    for spec in WINDY_MODELS:
        info = per_model[spec.id]
        if spec.met_only:
            continue
        if mode == "adjusted":
            sc = info.get("adjusted")
            pts = sc["points"] if sc else []
            # Correction F P0-4: adjusted mode uses EXCLUSIVELY scenario points
            # scenario points carry adjusted_mw only — energy = MW x interval_hours
            hourly_prod_points[spec.id] = [
                {
                    "valid_at_utc": p["valid_at_utc"],
                    "mw": p.get("adjusted_mw"),
                    "mwh": (p.get("adjusted_mw") * float(p.get("interval_hours") or 1.0)) if p.get("adjusted_mw") is not None else None,
                    "interval_hours": p.get("interval_hours", 1.0),
                }
                for p in pts
            ]
            rows = _daily_energy(hourly_prod_points[spec.id], tz, "mwh") if hourly_prod_points.get(spec.id) else []
            mw_points[spec.id] = [{"valid_at_utc": p["valid_at_utc"], "mw_for_avg": p.get("adjusted_mw"), "interval_hours": p.get("interval_hours", 1.0)} for p in pts]
        else:
            rows = _daily_energy(info["points"], tz, "interval_energy_mwh") if info["points"] else []
            hourly_prod_points[spec.id] = [
                {"valid_at_utc": p["valid_at_utc"], "mw": p.get("modeled_power_mw"), "mwh": p.get("interval_energy_mwh"), "interval_hours": p.get("interval_hours", 1.0)}
                for p in info["points"]
            ]
            mw_points[spec.id] = [{"valid_at_utc": p["valid_at_utc"], "mw_for_avg": p.get("modeled_power_mw"), "interval_hours": p.get("interval_hours", 1.0)} for p in info["points"]]
        info["daily_rows"] = rows
        for r in rows:
            daily_map.setdefault(r["date"], {})[spec.id] = r["mwh"] if r["has_data"] else None

    # continuous local calendar over the data span (UI shows blank cells for
    # days no model covers), anchored at today exactly like visibleDailyRows
    if daily_map:
        d0 = datetime.strptime(min(daily_map.keys()), "%Y-%m-%d")
        d1 = datetime.strptime(max(daily_map.keys()), "%Y-%m-%d")
        all_dates = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]
    else:
        all_dates = []
    today = datetime.now(tz).strftime("%Y-%m-%d")
    idx = next((i for i, d in enumerate(all_dates) if d >= today), None)
    start = idx if idx is not None else max(len(all_dates) - days, 0)
    shown_dates = all_dates[start : start + days]

    avg_models_eff = [m for m in avg_models if m in mw_points] if avg_enabled else []
    avg_rows = {r["date"]: r for r in _hourly_aligned_daily_avg(mw_points, tz, avg_models_eff)}

    prod_models = [m for m in WINDY_MODELS if not m.met_only]
    wb = Workbook()

    # ---- Sheet 1: Dnevna prognoza ----
    ws = wb.active
    ws.title = _sanitize_sheet_name("Dnevna prognoza")
    ws.append(["Datum"] + [m.label for m in prod_models] + ["Prosjek", "N min", "N max"])
    for date in shown_dates:
        vals = daily_map.get(date, {})
        ar = avg_rows.get(date)
        # Correction F P1-9: N min/N max = per-HOUR contributor range from the
        # ensemble (staggered coverage), not the count of models with daily MWh
        ws.append([date] + [round(vals[m.id] * factor, 6) if vals.get(m.id) is not None else None for m in prod_models] + [round(ar["mwh"] * factor, 6) if ar else None, ar["n_min"] if ar else None, ar["n_max"] if ar else None])
    total_row = ws.max_row + 1
    ws.append(["UKUPNO"])
    for ci, m in enumerate(prod_models, start=2):
        nums = [ws.cell(row=r, column=ci).value for r in range(2, total_row)]
        nums = [v for v in nums if isinstance(v, (int, float))]
        if nums:
            ws.cell(row=total_row, column=ci, value=round(sum(nums), 6))  # sum of ALREADY converted cells
    ar_vals = [avg_rows[d]["mwh"] * factor for d in shown_dates if d in avg_rows]
    if ar_vals:
        ws.cell(row=total_row, column=len(prod_models) + 2, value=round(sum(ar_vals), 6))
    for r in range(2, total_row + 1):
        for ci in range(2, len(prod_models) + 4):
            if isinstance(ws.cell(row=r, column=ci).value, (int, float)):
                ws.cell(row=r, column=ci).number_format = "0.0000" if unit == "gwh" else "0.00"
            if r == total_row:
                ws.cell(row=r, column=ci).font = TOTAL_FONT
    ws.freeze_panes = "A2"
    for ci in range(1, len(prod_models) + 4):
        ws.cell(row=1, column=ci).fill = HEADER_FILL
        ws.cell(row=1, column=ci).font = HEADER_FONT
        ws.column_dimensions[get_column_letter(ci)].width = 16

    # ---- Sheets 2/3: hourly wind + production ----
    def _hourly_sheet(title: str, models, picker) -> None:
        s = wb.create_sheet(_sanitize_sheet_name(title))
        s.append(["Sat (UTC)", f"Lokalno ({site['timezone']})", "Offset"] + [m.label for m in models])
        grid = set(shown_dates)
        by_key: dict[str, dict[str, object]] = {}
        local_by_key: dict[str, tuple[str, str]] = {}
        for m in models:
            for p in per_model[m.id]["points"]:
                dt = _parse_utc(p["valid_at_utc"])
                if dt.astimezone(tz).strftime("%Y-%m-%d") not in grid:
                    continue
                key = p["valid_at_utc"][:13]
                by_key.setdefault(key, {})[m.id] = picker(m, p)
                if key not in local_by_key:
                    local = dt.astimezone(tz)
                    off = local.strftime("%z")
                    local_by_key[key] = (local.strftime("%Y-%m-%d %H:%M"), f"UTC{off[:3]}:{off[3:]}")
        for key in sorted(by_key):
            loc, off = local_by_key.get(key, (key, "UTC"))
            s.append([key + ":00:00Z", loc, off] + [by_key[key].get(m.id) for m in models])
        s.freeze_panes = "A2"
        for ci in range(1, len(models) + 4):
            s.cell(row=1, column=ci).fill = HEADER_FILL
            s.cell(row=1, column=ci).font = HEADER_FONT
            s.column_dimensions[get_column_letter(ci)].width = 18

    _hourly_sheet("Satni vjetar", list(WINDY_MODELS), lambda m, p: p.get("wind_speed_mps"))
    # Correction F P0-4: production sheet sources adjusted points EXCLUSIVELY
    # in adjusted mode (never archived baseline under the adjusted label) and
    # carries BOTH MW and energy columns per model. Energy values are raw MWh
    # converted to the display unit at the end (factor), blanks for missing.
    s3 = wb.create_sheet(_sanitize_sheet_name("Satna proizvodnja"))
    prod_models_e = [m for m in WINDY_MODELS if not m.met_only]
    unit_hdr = unit.upper()
    s3.append(["Sat (UTC)", f"Lokalno ({site['timezone']})", "Offset"]
              + [f"{m.label} MW" for m in prod_models_e]
              + [f"{m.label} energija ({unit_hdr})" for m in prod_models_e])
    grid = set(shown_dates)
    by_key: dict[str, dict[str, object]] = {}
    local_by_key: dict[str, tuple[str, str]] = {}
    for m in prod_models_e:
        for p in hourly_prod_points.get(m.id, []):
            dt = _parse_utc(p["valid_at_utc"])
            if dt.astimezone(tz).strftime("%Y-%m-%d") not in grid:
                continue
            key = p["valid_at_utc"][:13]
            slot = by_key.setdefault(key, {})
            mw, mwh = p.get("mw"), p.get("mwh")
            slot[f"{m.id}:mw"] = mw
            slot[f"{m.id}:mwh"] = round(mwh * factor, 6) if mwh is not None else None
            if key not in local_by_key:
                local = dt.astimezone(tz)
                off = local.strftime("%z")
                local_by_key[key] = (local.strftime("%Y-%m-%d %H:%M"), f"UTC{off[:3]}:{off[3:]}")
    for key in sorted(by_key):
        loc, off = local_by_key.get(key, (key, "UTC"))
        row = [key + ":00:00Z", loc, off]
        for m in prod_models_e:
            row.append(by_key[key].get(f"{m.id}:mw"))
        for m in prod_models_e:
            row.append(by_key[key].get(f"{m.id}:mwh"))
        s3.append(row)
    s3.freeze_panes = "A2"
    for ci in range(1, len(prod_models_e) * 2 + 4):
        s3.cell(row=1, column=ci).fill = HEADER_FILL
        s3.cell(row=1, column=ci).font = HEADER_FONT
        s3.column_dimensions[get_column_letter(ci)].width = 20

    # ---- Sheet 4: Pokrivenost ----
    s4 = wb.create_sheet(_sanitize_sheet_name("Pokrivenost"))
    s4.append(["model_id", "label", "provider", "met_only", "visine_m", "metoda", "request_days", "max_lead_h", "status", "razlog", "n_points", "n_wind_valid", "run_issued_utc", "fetched_at_utc"])
    for m in WINDY_MODELS:
        info = per_model[m.id]
        s4.append(
            [
                m.id,
                m.label,
                m.provider,
                "DA" if m.met_only else "NE",
                ",".join(str(h) for h in m.speed_heights),
                m.height_method,
                m.request_days,
                m.max_lead_hours,
                info["status"],
                _safe_str(info.get("reason")),
                info.get("n_points"),
                info.get("n_wind_valid"),
                info.get("run_issued_utc"),
                info.get("fetched_at_utc"),
            ]
        )
    for ci in range(1, 15):
        s4.cell(row=1, column=ci).fill = HEADER_FILL
        s4.cell(row=1, column=ci).font = HEADER_FONT
        s4.column_dimensions[get_column_letter(ci)].width = 20

    # ---- Sheet 5: Metapodaci ----
    s5 = wb.create_sheet(_sanitize_sheet_name("Metapodaci"))
    meta = [
        ("site_id", _safe_str(site_id)),
        ("site_name", _safe_str(site.get("name"))),
        ("latitude", site.get("latitude")),
        ("longitude", site.get("longitude")),
        ("timezone", site["timezone"]),
        ("export_generated_at_utc", generated_at_utc),
        ("days", days),
        ("date_window", f"{shown_dates[0]} .. {shown_dates[-1]}" if shown_dates else "nema podataka"),
        ("mode", mode),
        ("energy_unit", unit),
        ("conversion", "1 GWh = 1000 MWh (prikazna konverzija na kraju; raw ostaje MWh)"),
        ("turbine_count", site.get("turbine_count")),
        ("turbine_rated_mw", site.get("turbine_rated_mw")),
        ("installed_mw", site.get("capacity_mw")),
        ("power_curve", _safe_str(site.get("power_curve_id"))),
        ("curve_status", "REFERENCE/UNVERIFIED (skalirana referentna krivulja, nije as-built)"),
        ("average_models", ",".join(avg_models)),
        ("average_enabled", "DA" if avg_enabled else "NE (Prosjek iskljucen u UI-u — Prosjek stupac prazan)"),
        ("average_rule", "hourly-aligned ensemble: prosjek MW po UTC segmentu x sati, dnevno agregirano u site TZ"),
        ("ukmo_note", "UKMO je 10 m povrsinski vjetar — NIJE hub vjetar i NIKAD nema MWh"),
        ("missing_note", "Prazna celija = nema podatka (nikad 0); 0 = stvarna modelirana nula"),
        ("disclaimer", "Sve vrijednosti su MODELIRANE procjene, ne mjerenja; nisu kalibrirana komercijalna prognoza"),
    ]
    for k, v in meta:
        s5.append([_safe_str(k), v])
    s5.column_dimensions["A"].width = 28
    s5.column_dimensions["B"].width = 72

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {"shown_dates": shown_dates, "per_model": {k: v["status"] for k, v in per_model.items()}}


def dbm_run(db_path, site_id: str, model_id: str):
    from . import db as dbm

    with dbm.session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM forecast_runs WHERE site_id=? AND model_id=? AND status='ok' "
            "ORDER BY COALESCE(run_issued_utc, dedupe_key) DESC LIMIT 1",
            (site_id, model_id),
        ).fetchone()
        return dict(row) if row else None


def dbm_points(db_path, run_id: int) -> list[dict]:
    from . import db as dbm

    with dbm.session(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc", (run_id,)).fetchall()]
