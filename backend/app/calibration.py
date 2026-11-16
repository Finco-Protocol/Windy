"""Optional, explicitly-labelled per-site calibration of forecast MWh.

Deliberately minimal and explainable: one robust multiplicative factor per
model (median of actual/modelled over training hours, bounded to
[FACTOR_MIN, FACTOR_MAX]). No ML model, no per-band fitting in this version.

Integrity rules:

- Training requires imported REAL measured actuals joined to archived
  known-issue forecasts, a minimum number of matched hours, and a validation
  holdout that is strictly LATER in time than the training window (chronological
  80/20 split) — no leakage, in-sample vs out-of-sample clearly reported.
- Calibration is a DERIVED series: stored forecast runs, points and raw
  payloads are never modified. Applying a factor multiplies forecast values in
  accuracy metrics/overlays only (see accuracy.use_calibration).
- With insufficient data the stored status is `not-calibrated-insufficient`
  and the factor is 1.0 — the UI shows `NOT CALIBRATED / insufficient actuals`
  instead of inventing a correction.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from . import db as dbm
from .accuracy import collect_best_pairs

MIN_TRAIN_HOURS = 200
MIN_VAL_HOURS = 72
FACTOR_MIN = 0.5
FACTOR_MAX = 1.5
# Ratio samples need a non-trivial forecast value to be meaningful.
MIN_FORECAST_MWH = 0.01


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upsert(conn, site_id: str, model_id: str, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO calibrations (site_id, model_id, factor, n_train_hours, n_val_hours,
                                  val_mae_before, val_mae_after, val_wape_before, val_wape_after,
                                  trained_at_utc, status, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site_id, model_id) DO UPDATE SET
            factor=excluded.factor, n_train_hours=excluded.n_train_hours,
            n_val_hours=excluded.n_val_hours, val_mae_before=excluded.val_mae_before,
            val_mae_after=excluded.val_mae_after, val_wape_before=excluded.val_wape_before,
            val_wape_after=excluded.val_wape_after, trained_at_utc=excluded.trained_at_utc,
            status=excluded.status, note=excluded.note
        """,
        (
            site_id,
            model_id,
            row["factor"],
            row["n_train_hours"],
            row["n_val_hours"],
            row.get("val_mae_before"),
            row.get("val_mae_after"),
            row.get("val_wape_before"),
            row.get("val_wape_after"),
            row["trained_at_utc"],
            row["status"],
            row["note"],
        ),
    )


def _val_metrics(val_pairs: list[dict], factor: float) -> tuple[float, float]:
    """(MAE MW, WAPE energy) on the holdout with the given factor applied."""
    n = len(val_pairs)
    abs_mw = 0.0
    abs_mwh = 0.0
    total_actual = 0.0
    for p in val_pairs:
        f_mw = p["f_mw"] * factor
        f_mwh = p["f_mwh"] * factor
        abs_mw += abs(f_mw - p["a_mwh"])
        abs_mwh += abs(f_mwh - p["a_mwh"])
        total_actual += p["a_mwh"]
    mae = abs_mw / n if n else None
    wape = abs_mwh / total_actual if n and total_actual > 0 else None
    return mae, wape


def train(db_path, site_id: str, from_utc: str, to_utc: str) -> dict:
    """Train per-model factors over archived forecasts ∩ complete actual hours."""
    collected = collect_best_pairs(db_path, site_id, from_utc, to_utc)
    counts = collected["counts"]
    trained_at = _now()
    results: dict[str, dict] = {}

    with dbm.session(db_path) as conn:
        for model_id, pairs in sorted(collected["per_model"].items()):
            if not pairs:
                _upsert(
                    conn, site_id, model_id,
                    {"factor": 1.0, "n_train_hours": 0, "n_val_hours": 0, "trained_at_utc": trained_at,
                     "status": "not-calibrated-insufficient",
                     "note": "no matched (archived forecast, complete actual) hours in range"},
                )
                results[model_id] = {"status": "not-calibrated-insufficient", "n_train_hours": 0, "n_val_hours": 0}
                continue

            # chronological split: holdout strictly later in time than training
            split = int(len(pairs) * 0.8)
            train_pairs, val_pairs = pairs[:split], pairs[split:]
            ratios = [p["a_mwh"] / p["f_mwh"] for p in train_pairs if p["f_mwh"] > MIN_FORECAST_MWH]
            if len(train_pairs) < MIN_TRAIN_HOURS or len(val_pairs) < MIN_VAL_HOURS or len(ratios) < MIN_TRAIN_HOURS // 2:
                _upsert(
                    conn, site_id, model_id,
                    {"factor": 1.0, "n_train_hours": len(train_pairs), "n_val_hours": len(val_pairs),
                     "trained_at_utc": trained_at, "status": "not-calibrated-insufficient",
                     "note": f"insufficient matched hours (train {len(train_pairs)}/{MIN_TRAIN_HOURS}, "
                             f"holdout {len(val_pairs)}/{MIN_VAL_HOURS} required)"},
                )
                results[model_id] = {
                    "status": "not-calibrated-insufficient",
                    "n_train_hours": len(train_pairs),
                    "n_val_hours": len(val_pairs),
                }
                continue

            raw_factor = statistics.median(ratios)
            factor = round(min(max(raw_factor, FACTOR_MIN), FACTOR_MAX), 4)
            mae_before, wape_before = _val_metrics(val_pairs, 1.0)
            mae_after, wape_after = _val_metrics(val_pairs, factor)
            _upsert(
                conn, site_id, model_id,
                {
                    "factor": factor, "n_train_hours": len(train_pairs), "n_val_hours": len(val_pairs),
                    "val_mae_before": mae_before, "val_mae_after": mae_after,
                    "val_wape_before": wape_before, "val_wape_after": wape_after,
                    "trained_at_utc": trained_at, "status": "calibrated",
                    "note": (
                        f"robust median factor {factor} (raw {raw_factor:.4f}, bounded to "
                        f"[{FACTOR_MIN}, {FACTOR_MAX}]); chronological 80/20 split, holdout strictly "
                        "later in time; derived series only — archived forecasts untouched"
                    ),
                },
            )
            results[model_id] = {
                "status": "calibrated",
                "factor": factor,
                "n_train_hours": len(train_pairs),
                "n_val_hours": len(val_pairs),
                "val_mae_before": mae_before,
                "val_mae_after": mae_after,
                "val_wape_before": wape_before,
                "val_wape_after": wape_after,
            }

    return {
        "site_id": site_id,
        "from_utc": collected["from_utc"],
        "to_utc": collected["to_utc"],
        "actual_hours": counts,
        "models": results,
        "note": (
            "Optional explainable calibration: one bounded multiplicative factor per model "
            "(median of actual/modelled on the training window). Archived forecast values are "
            "never modified; factors apply to derived views only."
        ),
    }


def get_calibrations(db_path, site_id: str) -> list[dict]:
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT model_id, factor, n_train_hours, n_val_hours, val_mae_before, val_mae_after, "
            "val_wape_before, val_wape_after, trained_at_utc, status, note "
            "FROM calibrations WHERE site_id=? ORDER BY model_id",
            (site_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def calibration_factors(db_path, site_id: str) -> dict[str, float]:
    """Only models with a successful calibration contribute a factor."""
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT model_id, factor FROM calibrations WHERE site_id=? AND status='calibrated'",
            (site_id,),
        ).fetchall()
    return {r["model_id"]: r["factor"] for r in rows}
