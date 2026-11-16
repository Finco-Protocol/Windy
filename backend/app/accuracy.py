"""Forecast-vs-actual accuracy: MAE (MW), bias (MW), WAPE (energy), coverage.

Interval alignment (v1.1): actual meter intervals (15/30/60-min or other) are
aggregated to **complete UTC hours** before any comparison. A complete hour is
one fully covered by measured intervals (e.g. 4×15 min or 2×30 min); partial
hours are counted and reported but never scored, and intervals that straddle
an hour boundary are reported as unaligned rather than force-fitted. A 1-hour
forecast value is therefore never casually matched against a 15-minute reading.

Strict comparison rules (unchanged from v1):

- Only **archived runs with a known issue time** participate.
- Forecast points pair only with the actual aggregate of the *same valid hour*;
  `valid_at > run_issued` (no leakage of actuals into past predictions).
- Lead time = valid_at − run_issued, bucketed day-ahead … 7-day.
- Best-issue selection: for each (model, valid hour, bin) the pair from the
  LATEST archived issue wins — deterministic, no double counting.

Calibration (optional, `use_calibration`): applies the stored per-model
multiplicative factor to forecast MW/MWh **in metrics only** — archived values
are never rewritten; the response reports which factors were applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db as dbm

# (lead_from_h, lead_to_h_exclusive, label)
LEAD_BINS: tuple[tuple[int, int, str], ...] = (
    (0, 24, "day-ahead (0-24h)"),
    (24, 48, "2-day ahead (24-48h)"),
    (48, 72, "3-day ahead (48-72h)"),
    (72, 120, "5-day ahead (72-120h)"),
    (120, 168, "7-day ahead (120-168h)"),
)
BIN_LABELS = [label for _, _, label in LEAD_BINS]


def lead_bin_label(lead_hours: float) -> str | None:
    for lo, hi, label in LEAD_BINS:
        if lo <= lead_hours < hi:
            return label
    return None  # beyond 168 h: not binned


def parse_z(s: str) -> str:
    """Normalize an incoming ISO-ish UTC bound to the canonical 'Z' form."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime_from_iso(s)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def datetime_from_iso(s: str):
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _lead_hours(issued: str, valid: str) -> float | None:
    try:
        return (datetime_from_iso(valid) - datetime_from_iso(issued)).total_seconds() / 3600.0
    except ValueError:
        return None


# --- actual-side hourly aggregation ---------------------------------------------


def hourly_actuals(conn, site_id: str, from_utc: str, to_utc: str) -> tuple[dict[str, dict], int]:
    """Aggregate measured intervals into UTC hours.

    An interval contributes to the hour of its start when it lies fully inside
    that hour: [start, end) ⊆ [hour_floor, hour_floor + 1h). A full-hour
    interval 00:00→01:00 therefore belongs to hour 00 (end == boundary is
    allowed); anything straddling a boundary (e.g. 00:10→01:10) or longer than
    an hour is counted as unaligned and excluded — reported, never silently
    redistributed. A hour with total coverage >= 0.999 h is `complete`.

    Returns ({hour_prefix "YYYY-MM-DDTHH": {energy_mwh, covered_hours, n, complete}},
    n_unaligned_intervals).
    """
    from datetime import timedelta as _timedelta

    rows = conn.execute(
        "SELECT interval_start_utc, interval_end_utc, energy_mwh FROM actual_points "
        "WHERE site_id=? AND interval_start_utc >= ? AND interval_start_utc < ?",
        (site_id, from_utc, to_utc),
    ).fetchall()
    hours: dict[str, dict] = {}
    unaligned = 0
    for r in rows:
        start = datetime_from_iso(r["interval_start_utc"])
        end = datetime_from_iso(r["interval_end_utc"])
        hour_floor = start.replace(minute=0, second=0, microsecond=0)
        hour_ceil = hour_floor + _timedelta(hours=1)
        if end <= start or end > hour_ceil:
            unaligned += 1
            continue
        duration = (end - start).total_seconds() / 3600.0
        prefix = hour_floor.strftime("%Y-%m-%dT%H")
        slot = hours.setdefault(prefix, {"energy_mwh": 0.0, "covered_hours": 0.0, "n": 0})
        slot["energy_mwh"] += r["energy_mwh"] or 0.0
        slot["covered_hours"] += duration
        slot["n"] += 1
    for slot in hours.values():
        slot["complete"] = slot["covered_hours"] >= 0.999
    return hours, unaligned


def hour_stamp(hour_prefix: str) -> str:
    return f"{hour_prefix}:00:00Z"


# --- shared pair collection ------------------------------------------------------


def collect_best_pairs(db_path, site_id: str, from_utc: str, to_utc: str) -> dict:
    """Best-issue forecast/actual pairs per model over complete actual hours.

    Returns {per_model: {model_id: [{valid_at, run_issued_utc, f_mw, f_mwh, a_mwh}]},
    actual_hours: {...}, counts: {complete, partial, unaligned}}.
    """
    f = parse_z(from_utc)
    t = parse_z(to_utc)
    with dbm.session(db_path) as conn:
        hours, unaligned = hourly_actuals(conn, site_id, f, t)
        forecast_rows = conn.execute(
            """
            SELECT fr.model_id, fr.run_issued_utc, fp.valid_at_utc,
                   fp.modeled_power_mw, fp.interval_energy_mwh
            FROM forecast_points fp
            JOIN forecast_runs fr ON fr.id = fp.run_id
            WHERE fr.site_id = ?
              AND fr.run_issued_utc IS NOT NULL
              AND fp.valid_at_utc >= ? AND fp.valid_at_utc < ?
              AND fp.valid_at_utc > fr.run_issued_utc
              AND fp.modeled_power_mw IS NOT NULL
              AND fp.interval_energy_mwh IS NOT NULL
            ORDER BY fr.model_id, fp.valid_at_utc, fr.run_issued_utc
            """,
            (site_id, f, t),
        ).fetchall()

    complete_hours = {h: s for h, s in hours.items() if s["complete"]}
    partial = sum(1 for s in hours.values() if not s["complete"])

    # best issue per (model, valid hour): latest issued wins
    best: dict[tuple[str, str], dict] = {}
    for r in forecast_rows:
        hour_prefix = r["valid_at_utc"][:13]
        if hour_prefix not in complete_hours:
            continue
        key = (r["model_id"], hour_prefix)
        prev = best.get(key)
        if prev is None or r["run_issued_utc"] > prev["run_issued_utc"]:
            best[key] = {
                "valid_at": hour_stamp(hour_prefix),
                "run_issued_utc": r["run_issued_utc"],
                "f_mw": r["modeled_power_mw"],
                "f_mwh": r["interval_energy_mwh"],
                "a_mwh": complete_hours[hour_prefix]["energy_mwh"],
            }

    per_model: dict[str, list[dict]] = {}
    for (model_id, _h), item in best.items():
        per_model.setdefault(model_id, []).append(item)
    for pairs in per_model.values():
        pairs.sort(key=lambda p: p["valid_at"])

    return {
        "per_model": per_model,
        "actual_hours": hours,
        "counts": {
            "complete": len(complete_hours),
            "partial": partial,
            "unaligned": unaligned,
        },
        "from_utc": f,
        "to_utc": t,
    }


def _load_factors(db_path, site_id: str) -> dict[str, float]:
    try:
        from .calibration import calibration_factors

        return calibration_factors(db_path, site_id)
    except Exception:
        return {}


def _apply_factor(pair: dict, factor: float) -> dict:
    scaled = dict(pair)
    scaled["f_mw"] = pair["f_mw"] * factor
    scaled["f_mwh"] = pair["f_mwh"] * factor
    return scaled


@dataclass
class BinMetrics:
    n: int = 0
    abs_err_mw: float = 0.0
    err_mw: float = 0.0
    abs_err_mwh: float = 0.0
    actual_mwh: float = 0.0
    forecast_mwh: float = 0.0
    hours: set = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.hours is None:
            self.hours = set()

    def add(self, f_mw: float, a_mw: float, f_mwh: float, a_mwh: float, valid_at: str) -> None:
        self.n += 1
        self.abs_err_mw += abs(f_mw - a_mw)
        self.err_mw += f_mw - a_mw
        self.abs_err_mwh += abs(f_mwh - a_mwh)
        self.actual_mwh += a_mwh
        self.forecast_mwh += f_mwh
        self.hours.add(valid_at)

    def to_dict(self, possible_hours: int) -> dict:
        n = self.n
        return {
            "n_pairs": n,
            "mae_mw": round(self.abs_err_mw / n, 4) if n else None,
            "bias_mw": round(self.err_mw / n, 4) if n else None,
            "wape_energy": round(self.abs_err_mwh / self.actual_mwh, 4) if n and self.actual_mwh > 0 else None,
            "actual_energy_mwh": round(self.actual_mwh, 3) if n else None,
            "forecast_energy_mwh": round(self.forecast_mwh, 3) if n else None,
            "distinct_hours": len(self.hours),
            "coverage": round(len(self.hours) / possible_hours, 4) if possible_hours else None,
        }


def accuracy(db_path, site_id: str, from_utc: str, to_utc: str, use_calibration: bool = False) -> dict:
    """Per-model, per-lead-bin MAE/bias/WAPE over archived runs ∩ complete actual hours."""
    if parse_z(to_utc) <= parse_z(from_utc):
        raise ValueError("to_utc must be after from_utc")
    collected = collect_best_pairs(db_path, site_id, from_utc, to_utc)
    counts = collected["counts"]
    n_complete = counts["complete"]
    factors = _load_factors(db_path, site_id) if use_calibration else {}
    applied: dict[str, float] = {}

    models: dict[str, dict] = {}
    for model_id, pairs in collected["per_model"].items():
        factor = factors.get(model_id, 1.0)
        if use_calibration and model_id in factors:
            applied[model_id] = factor
        bins = {label: BinMetrics() for label in BIN_LABELS}
        for pair in pairs:
            lead = _lead_hours(pair["run_issued_utc"], pair["valid_at"])
            if lead is None:
                continue
            label = lead_bin_label(lead)
            if label is None:
                continue
            item = _apply_factor(pair, factor) if factor != 1.0 else pair
            # a complete hour's energy (MWh) equals its average power (MW)
            bins[label].add(item["f_mw"], item["a_mwh"], item["f_mwh"], item["a_mwh"], item["valid_at"])
        bins_out: dict[str, dict] = {}
        total = BinMetrics()
        for label in BIN_LABELS:
            m = bins[label]
            if m.n == 0:
                continue
            total.abs_err_mw += m.abs_err_mw
            total.err_mw += m.err_mw
            total.abs_err_mwh += m.abs_err_mwh
            total.actual_mwh += m.actual_mwh
            total.forecast_mwh += m.forecast_mwh
            total.n += m.n
            total.hours |= m.hours
            bins_out[label] = m.to_dict(n_complete)
        models[model_id] = {"bins": bins_out, "total": total.to_dict(n_complete) if total.n else None}

    return {
        "site_id": site_id,
        "from_utc": collected["from_utc"],
        "to_utc": collected["to_utc"],
        "actual_hours_in_range": n_complete,
        "actual_hours_partial": counts["partial"],
        "actual_intervals_unaligned": counts["unaligned"],
        "lead_bins": BIN_LABELS,
        "models": models,
        "calibration_applied": applied,
        "has_measured_actuals": n_complete + counts["partial"] + counts["unaligned"] > 0,
        "note": (
            "Actual meter intervals are aggregated to complete UTC hours (partial/unaligned "
            "hours are reported, never scored). MAE/bias in MW; WAPE over hourly energy (MWh). "
            "Only archived runs with a known issue time and valid_at strictly after the issue "
            "are compared — no reanalysis or current-forecast substitution."
        ),
    }


def overlay(
    db_path,
    site_id: str,
    from_utc: str,
    to_utc: str,
    lead_bin: str | None = None,
    use_calibration: bool = False,
) -> dict:
    """Hourly aligned series: measured actual energy vs each model's archived
    forecast energy for the same valid hour (optionally one lead bin)."""
    if lead_bin is not None and lead_bin not in BIN_LABELS:
        raise ValueError(f"lead_bin must be one of {BIN_LABELS}")
    if parse_z(to_utc) <= parse_z(from_utc):
        raise ValueError("to_utc must be after from_utc")
    collected = collect_best_pairs(db_path, site_id, from_utc, to_utc)
    factors = _load_factors(db_path, site_id) if use_calibration else {}

    by_valid: dict[str, dict] = {}
    for hour_prefix, slot in collected["actual_hours"].items():
        entry = by_valid.setdefault(hour_stamp(hour_prefix), {"valid_at_utc": hour_stamp(hour_prefix)})
        if slot["complete"]:
            entry["actual_mwh"] = round(slot["energy_mwh"], 4)
            entry["actual_mw"] = round(slot["energy_mwh"], 4)  # MWh over a complete hour == average MW
        else:
            entry["actual_partial"] = True
            entry["actual_covered_hours"] = round(slot["covered_hours"], 4)

    for model_id, pairs in collected["per_model"].items():
        factor = factors.get(model_id, 1.0)
        for pair in pairs:
            if lead_bin is not None:
                lead = _lead_hours(pair["run_issued_utc"], pair["valid_at"])
                if lead is None or lead_bin_label(lead) != lead_bin:
                    continue
            value = pair["f_mwh"] * factor
            by_valid.setdefault(pair["valid_at"], {"valid_at_utc": pair["valid_at"]}).setdefault("models", {})[model_id] = round(value, 4)

    return {
        "site_id": site_id,
        "from_utc": collected["from_utc"],
        "to_utc": collected["to_utc"],
        "lead_bin": lead_bin,
        "hours": [by_valid[v] for v in sorted(by_valid)],
        "calibration_applied": factors if use_calibration else {},
        "note": (
            "Actuals are measured generation aggregated to complete UTC hours; per-model "
            "values are archived forecast issues (never reanalysis). Hours flagged "
            "actual_partial have incomplete meter coverage and are not scored."
        ),
    }
