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


def hourly_actuals(conn, site_id: str, from_utc: str, to_utc: str, capacity_mw: float | None = None) -> tuple[dict[str, dict], dict]:
    """Aggregate measured intervals into UTC hours with strict coverage rules.

    A scored hour must be covered by a **non-overlapping continuous union** of
    measured intervals that starts at the hour boundary and ends at the next
    one (tolerance 1 s). Intervals may be 15/30/60 minutes or mixed. An hour is
    excluded — with an explicit reason — when intervals overlap, leave a gap,
    miss a boundary, cross an hour boundary, have invalid duration, contain
    non-finite readings, or exceed a sensible physical limit
    (`power_mw > capacity_mw × 1.05` when capacity is known). Energy is never
    invented and stored data is never modified: bad hours are simply not
    scoreable.

    Returns ({hour_prefix: {energy_mwh, covered_hours, n, complete, reason}},
    counts {unaligned, invalid, over_limit}).
    """
    from datetime import timedelta as _timedelta

    tol = 1.0 / 3600.0  # 1 second, in hours
    rows = conn.execute(
        "SELECT interval_start_utc, interval_end_utc, energy_mwh, power_mw FROM actual_points "
        "WHERE site_id=? AND interval_start_utc >= ? AND interval_start_utc < ?",
        (site_id, from_utc, to_utc),
    ).fetchall()

    hours: dict[str, dict] = {}
    counts = {"unaligned": 0, "invalid": 0, "over_limit": 0}

    for r in rows:
        start = datetime_from_iso(r["interval_start_utc"])
        end = datetime_from_iso(r["interval_end_utc"])
        energy = r["energy_mwh"]
        power = r["power_mw"]
        hour_floor = start.replace(minute=0, second=0, microsecond=0)
        hour_ceil = hour_floor + _timedelta(hours=1)
        prefix = hour_floor.strftime("%Y-%m-%dT%H")
        slot = hours.setdefault(
            prefix,
            {"energy_mwh": 0.0, "covered_hours": 0.0, "n": 0, "complete": False, "reason": None, "intervals": []},
        )

        bad_duration = end <= start
        crosses_boundary = (not bad_duration) and end > hour_ceil
        nonfinite = energy is None or power is None or not (math_isfinite(energy) and math_isfinite(power))
        over_limit = capacity_mw is not None and power is not None and math_isfinite(power) and power > capacity_mw * 1.05

        if bad_duration or crosses_boundary or nonfinite or over_limit:
            if bad_duration or nonfinite:
                counts["invalid"] += 1
            elif crosses_boundary:
                counts["unaligned"] += 1
            else:
                counts["over_limit"] += 1
            slot["n"] += 1
            if slot["reason"] is None:
                if bad_duration:
                    slot["reason"] = "invalid duration"
                elif crosses_boundary:
                    slot["reason"] = "boundary-crossing interval"
                elif nonfinite:
                    slot["reason"] = "non-finite reading"
                else:
                    slot["reason"] = "reading exceeds site physical limit"
            continue

        slot["energy_mwh"] += energy
        slot["n"] += 1
        slot["intervals"].append((start, end, energy))

    for prefix, slot in hours.items():
        if slot["reason"] is not None:
            continue
        hour_start = datetime_from_iso(hour_stamp(prefix))
        hour_end = hour_start + _timedelta(hours=1)
        ivs = sorted(slot["intervals"])
        overlap = any(ivs[i + 1][0] < ivs[i][1] - _timedelta(seconds=1) for i in range(len(ivs) - 1))
        union_h = 0.0
        merged: list = []
        for s, e, _energy in ivs:
            if merged and s <= merged[-1][1] + _timedelta(seconds=1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        for s, e in merged:
            union_h += (e - s).total_seconds() / 3600.0

        if overlap:
            slot["reason"] = "overlapping intervals"
        elif union_h < 1.0 - tol:
            slot["reason"] = "gap in coverage"
        elif ivs and (ivs[0][0] > hour_start + _timedelta(seconds=1) or ivs[-1][1] < hour_end - _timedelta(seconds=1)):
            slot["reason"] = "hour boundary not covered"
        else:
            slot["complete"] = True
            slot["covered_hours"] = union_h
        slot.pop("intervals", None)

    complete = {h: s for h, s in hours.items() if s["complete"]}
    # keep only summary fields on the returned hours
    for s in hours.values():
        s.pop("intervals", None)
        s.setdefault("covered_hours", 0.0)
    return hours, counts


def math_isfinite(v) -> bool:
    import math

    return isinstance(v, (int, float)) and math.isfinite(v)


def hour_stamp(hour_prefix: str) -> str:
    return f"{hour_prefix}:00:00Z"


# --- shared pair collection ------------------------------------------------------


def collect_best_pairs(db_path, site_id: str, from_utc: str, to_utc: str, per_bin: bool = False) -> dict:
    """Forecast/actual pairs per model over complete actual hours.

    Selection policy (auditable, deterministic):

    - per_bin=True — the pair-selection key is (model, valid_hour, lead_bin):
      within each lead bin, the LATEST qualifying archived issue wins. The same
      actual hour can therefore contribute to the 1-, 2-, 3-, 5- and 7-day bins
      when issues at those leads actually exist; a recent day-ahead issue can
      never displace a genuine older 3-day prediction from its own bin.
    - per_bin=False (overall / calibration policy) — at most ONE forecast per
      (model, valid_hour): the latest known issue as of that hour. Overall
      totals are computed from this deduplicated set, never by summing bins.

    Both policies exclude issue-unknown forecasts, reanalysis and any issue
    at/after the valid time itself.
    """
    f = parse_z(from_utc)
    t = parse_z(to_utc)
    with dbm.session(db_path) as conn:
        capacity_row = conn.execute("SELECT capacity_mw FROM assets WHERE id=?", (site_id,)).fetchone()
        capacity = float(capacity_row["capacity_mw"]) if capacity_row else None
        hours, coverage_counts = hourly_actuals(conn, site_id, f, t, capacity_mw=capacity)
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

    counts = {
        "complete": len(complete_hours),
        "partial": partial,
        "unaligned": coverage_counts["unaligned"],
        "invalid": coverage_counts["invalid"],
        "over_limit": coverage_counts["over_limit"],
    }

    best: dict[tuple, dict] = {}
    for r in forecast_rows:
        hour_prefix = r["valid_at_utc"][:13]
        if hour_prefix not in complete_hours:
            continue
        lead = _lead_hours(r["run_issued_utc"], hour_stamp(hour_prefix))
        if lead is None:
            continue
        label = lead_bin_label(lead) if per_bin else ""
        key = (r["model_id"], hour_prefix, label)
        prev = best.get(key)
        if prev is None or r["run_issued_utc"] > prev["run_issued_utc"]:
            best[key] = {
                "valid_at": hour_stamp(hour_prefix),
                "run_issued_utc": r["run_issued_utc"],
                "lead_hours": lead,
                "f_mw": r["modeled_power_mw"],
                "f_mwh": r["interval_energy_mwh"],
                "a_mwh": complete_hours[hour_prefix]["energy_mwh"],
            }

    per_model: dict[str, list[dict]] = {}
    for (model_id, _h, _label), item in best.items():
        per_model.setdefault(model_id, []).append(item)
    for pairs in per_model.values():
        pairs.sort(key=lambda p: p["valid_at"])

    return {
        "per_model": per_model,
        "actual_hours": hours,
        "counts": counts,
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
    """Per-model metrics over archived runs ∩ complete actual hours.

    Bins use per-lead selection (the same actual hour may appear in several
    bins — each from its own latest qualifying issue). The `total` block is a
    DEDUPLICATED overall score: the latest known issue per valid hour only,
    never a sum over bins.
    """
    if parse_z(to_utc) <= parse_z(from_utc):
        raise ValueError("to_utc must be after from_utc")
    bin_collected = collect_best_pairs(db_path, site_id, from_utc, to_utc, per_bin=True)
    overall_collected = collect_best_pairs(db_path, site_id, from_utc, to_utc, per_bin=False)
    counts = bin_collected["counts"]
    n_complete = counts["complete"]
    factors = _load_factors(db_path, site_id) if use_calibration else {}
    applied: dict[str, float] = {}

    models: dict[str, dict] = {}
    all_models = sorted(set(bin_collected["per_model"]) | set(overall_collected["per_model"]))
    for model_id in all_models:
        factor = factors.get(model_id, 1.0)
        if use_calibration and model_id in factors:
            applied[model_id] = factor

        # per-lead-bin metrics: each bin independently selects its own pairs
        bins = {label: BinMetrics() for label in BIN_LABELS}
        for pair in bin_collected["per_model"].get(model_id, []):
            label = lead_bin_label(pair["lead_hours"])
            if label is None:
                continue
            item = _apply_factor(pair, factor) if factor != 1.0 else pair
            # a complete hour's energy (MWh) equals its average power (MW)
            bins[label].add(item["f_mw"], item["a_mwh"], item["f_mwh"], item["a_mwh"], item["valid_at"])
        bins_out: dict[str, dict] = {}
        for label in BIN_LABELS:
            m = bins[label]
            if m.n == 0:
                continue
            bins_out[label] = m.to_dict(n_complete)

        # overall: deduplicated (one forecast per valid hour), computed directly
        total = BinMetrics()
        for pair in overall_collected["per_model"].get(model_id, []):
            if lead_bin_label(pair["lead_hours"]) is None:
                continue
            item = _apply_factor(pair, factor) if factor != 1.0 else pair
            total.add(item["f_mw"], item["a_mwh"], item["f_mwh"], item["a_mwh"], item["valid_at"])
        models[model_id] = {"bins": bins_out, "total": total.to_dict(n_complete) if total.n else None}

    return {
        "site_id": site_id,
        "from_utc": bin_collected["from_utc"],
        "to_utc": bin_collected["to_utc"],
        "actual_hours_in_range": n_complete,
        "actual_hours_partial": counts["partial"],
        "actual_intervals_unaligned": counts["unaligned"],
        "actual_intervals_invalid": counts["invalid"],
        "actual_intervals_over_limit": counts["over_limit"],
        "lead_bins": BIN_LABELS,
        "models": models,
        "calibration_applied": applied,
        "has_measured_actuals": n_complete + counts["partial"] + counts["unaligned"] + counts["invalid"] > 0,
        "selection_policy": (
            "bins: latest archived issue per (model, valid hour, lead bin) — one hour may appear "
            "in several bins; overall: latest known issue per (model, valid hour), deduplicated"
        ),
        "note": (
            "A scored hour is a non-overlapping continuous union of measured intervals spanning "
            "the full UTC hour (gaps/overlaps/boundary misses are reported, never scored). "
            "MAE/bias in MW; WAPE over hourly energy (MWh). Only archived runs with a known "
            "issue time and valid_at strictly after the issue are compared — no reanalysis or "
            "current-forecast substitution."
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
    collected = collect_best_pairs(db_path, site_id, from_utc, to_utc, per_bin=lead_bin is not None)
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
            if slot.get("reason"):
                entry["actual_reason"] = slot["reason"]

    for model_id, pairs in collected["per_model"].items():
        factor = factors.get(model_id, 1.0)
        for pair in pairs:
            if lead_bin is not None and lead_bin_label(pair["lead_hours"]) != lead_bin:
                continue  # per-bin collection already filtered; double-guard
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
