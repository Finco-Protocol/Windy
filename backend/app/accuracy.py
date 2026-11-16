"""Forecast-vs-actual accuracy: MAE (MW), bias (MW), WAPE (energy), coverage.

Strict comparisons only:

- Only **archived runs with a known issue time** participate (the live-forecast
  fallback has issue=None and can never fake a past prediction).
- A forecast point is only compared when the actual for the *same valid time*
  exists — never the actuals of the issue date.
- Lead time = valid_at − run_issued (hours), bucketed into day-ahead … 7-day
  bins. No leakage: points with valid_at ≤ run_issued are excluded.
- Metrics over a model's own covered hours; `coverage` reports how much of the
  actual-covered time range each model's archived runs actually intersect.
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


_PAIRS_SQL = """
SELECT fr.model_id,
       fr.run_issued_utc,
       fr.fetched_at_utc,
       fp.valid_at_utc,
       fp.modeled_power_mw,
       fp.interval_energy_mwh,
       ap.energy_mwh  AS actual_energy_mwh,
       ap.power_mw    AS actual_power_mw
FROM forecast_points fp
JOIN forecast_runs fr ON fr.id = fp.run_id
JOIN actual_points ap
  ON ap.site_id = fr.site_id
 AND ap.interval_start_utc <= fp.valid_at_utc
 AND fp.valid_at_utc < ap.interval_end_utc
WHERE fr.site_id = ?
  AND fr.run_issued_utc IS NOT NULL
  AND fp.valid_at_utc >= ?
  AND fp.valid_at_utc < ?
  AND fp.valid_at_utc > fr.run_issued_utc
  AND fp.modeled_power_mw IS NOT NULL
  AND fp.interval_energy_mwh IS NOT NULL
  AND ap.power_mw IS NOT NULL
  AND ap.energy_mwh IS NOT NULL
ORDER BY fr.model_id, fp.valid_at_utc, fr.run_issued_utc
"""


def accuracy(db_path, site_id: str, from_utc: str, to_utc: str) -> dict:
    """Per-model, per-lead-bin MAE/bias/WAPE over archived runs ∩ actuals."""
    f = parse_z(from_utc)
    t = parse_z(to_utc)
    if t <= f:
        raise ValueError("to_utc must be after from_utc")

    with dbm.session(db_path) as conn:
        pairs = conn.execute(_PAIRS_SQL, (site_id, f, t)).fetchall()
        actual_hours = {
            r["interval_start_utc"]
            for r in conn.execute(
                "SELECT interval_start_utc FROM actual_points WHERE site_id=? AND interval_start_utc >= ? AND interval_start_utc < ?",
                (site_id, f, t),
            ).fetchall()
        }

    # Choose, per (model, valid hour, bin), the pair with the LATEST issue (the
    # freshest archived prediction for that lead window) — deterministic and
    # free of double counting when several runs cover the same hour.
    best: dict[tuple[str, str, str], dict] = {}
    for r in pairs:
        lead = _lead_hours(r["run_issued_utc"], r["valid_at_utc"])
        if lead is None:
            continue
        label = lead_bin_label(lead)
        if label is None:
            continue
        key = (r["model_id"], label, r["valid_at_utc"])
        prev = best.get(key)
        if prev is None or r["run_issued_utc"] > prev["run_issued_utc"]:
            best[key] = {
                "run_issued_utc": r["run_issued_utc"],
                "f_mw": r["modeled_power_mw"],
                "a_mw": r["actual_power_mw"],
                "f_mwh": r["interval_energy_mwh"],
                "a_mwh": r["actual_energy_mwh"],
                "valid_at": r["valid_at_utc"],
            }

    models: dict[str, dict] = {}
    for (model_id, label, _valid), item in best.items():
        mb = models.setdefault(model_id, {label: BinMetrics() for label in BIN_LABELS})
        mb[label].add(item["f_mw"], item["a_mw"], item["f_mwh"], item["a_mwh"], item["valid_at"])

    out: dict[str, dict] = {}
    for model_id, bins in models.items():
        total = BinMetrics()
        bins_out: dict[str, dict] = {}
        for label in BIN_LABELS:
            m = bins[label]
            if m.n == 0:
                continue
            total_abs = m  # pooled below
            total.abs_err_mw += m.abs_err_mw
            total.err_mw += m.err_mw
            total.abs_err_mwh += m.abs_err_mwh
            total.actual_mwh += m.actual_mwh
            total.forecast_mwh += m.forecast_mwh
            total.n += m.n
            total.hours |= m.hours
            bins_out[label] = m.to_dict(len(actual_hours))
        out[model_id] = {"bins": bins_out, "total": total.to_dict(len(actual_hours)) if total.n else None}

    return {
        "site_id": site_id,
        "from_utc": f,
        "to_utc": t,
        "actual_hours_in_range": len(actual_hours),
        "lead_bins": BIN_LABELS,
        "models": out,
        "note": (
            "MAE/bias in MW (interval-average); WAPE over interval energy (MWh). "
            "Only archived runs with a known issue time and valid_at strictly after "
            "the issue are compared — no substitution of current forecasts or "
            "reanalysis for past predictions."
        ),
    }


def _lead_hours(issued: str, valid: str) -> float | None:
    try:
        return (datetime_from_iso(valid) - datetime_from_iso(issued)).total_seconds() / 3600.0
    except ValueError:
        return None


def overlay(db_path, site_id: str, from_utc: str, to_utc: str, lead_bin: str | None = None) -> dict:
    """Hourly aligned series: actual energy vs each model's archived forecast
    energy for the same valid time (optionally restricted to one lead bin)."""
    f = parse_z(from_utc)
    t = parse_z(to_utc)
    if t <= f:
        raise ValueError("to_utc must be after from_utc")
    if lead_bin is not None and lead_bin not in BIN_LABELS:
        raise ValueError(f"lead_bin must be one of {BIN_LABELS}")

    with dbm.session(db_path) as conn:
        actuals = {
            r["interval_start_utc"]: {"energy_mwh": r["energy_mwh"], "power_mw": r["power_mw"]}
            for r in conn.execute(
                "SELECT interval_start_utc, energy_mwh, power_mw FROM actual_points "
                "WHERE site_id=? AND interval_start_utc >= ? AND interval_start_utc < ? ORDER BY interval_start_utc",
                (site_id, f, t),
            ).fetchall()
        }
        pairs = conn.execute(_PAIRS_SQL, (site_id, f, t)).fetchall()

    best: dict[tuple[str, str], dict] = {}
    for r in pairs:
        if lead_bin is not None:
            lead = _lead_hours(r["run_issued_utc"], r["valid_at_utc"])
            if lead is None or lead_bin_label(lead) != lead_bin:
                continue
        key = (r["model_id"], r["valid_at_utc"])
        prev = best.get(key)
        if prev is None or r["run_issued_utc"] > prev["run_issued_utc"]:
            best[key] = r

    hours: dict[str, dict] = {}
    for valid_at in sorted(set(actuals) | {v for _, v in best}):
        hours[valid_at] = {"valid_at_utc": valid_at}
        if valid_at in actuals:
            hours[valid_at]["actual_mwh"] = actuals[valid_at]["energy_mwh"]
            hours[valid_at]["actual_mw"] = actuals[valid_at]["power_mw"]
    for (model_id, valid_at), r in best.items():
        hours[valid_at].setdefault("models", {})[model_id] = r["interval_energy_mwh"]

    return {
        "site_id": site_id,
        "from_utc": f,
        "to_utc": t,
        "lead_bin": lead_bin,
        "hours": [hours[v] for v in sorted(hours)],
        "note": "Actuals are measured generation; per-model values are archived forecast issues (never reanalysis).",
    }
