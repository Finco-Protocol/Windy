"""Select a truthful, sufficiently covered forecast issue for the forecast screen.

The archive always keeps *both* latest short and older long runs, immutably.
This read-side policy chooses a recent long 00/12Z issue for long-horizon
models only when its *stored non-null wind* actually reaches the long horizon.
A shorter or truncated 00/12Z snapshot cannot masquerade as a full run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .registry import ModelSpec

# Provider-issued long-cycle rules; the live response must STILL prove coverage.
# 00/12 ECMWF IFS -> 360 h, 06/18 -> ~90 h; ICON Global 00/12 ->
# 180 h, 06/18 -> 120 h. Thresholds tolerate partial last intervals,
# without accepting 7-day or 5-day snapshots as complete long runs.
LONG_CYCLE_MIN_LEAD_HOURS = {"ecmwf_ifs": 300, "icon_global": 150}
MAX_LONG_ISSUE_AGE_VS_LATEST_HOURS = 30


def _date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except ValueError:
        return None


def _coverage(conn, run) -> tuple[str | None, float | None]:
    last = conn.execute(
        "SELECT MAX(valid_at_utc) AS t FROM forecast_points "
        "WHERE run_id=? AND wind_speed_mps IS NOT NULL",
        (run["id"],),
    ).fetchone()["t"]
    issued = _date(run["run_issued_utc"])
    last_dt = _date(last)
    lead = round((last_dt - issued).total_seconds() / 3600, 2) if issued and last_dt else None
    return last, lead


def select_forecast_run(conn, site_id: str, spec: ModelSpec):
    """Return (sqlite row or None, selection metadata).

    Never join / interpolate across issues. The short issue remains accessible
    in history, while the forecast screen prefers a verified recent long issue.
    """
    runs = conn.execute(
        "SELECT * FROM forecast_runs WHERE site_id=? AND model_id=? AND status='ok' "
        "ORDER BY COALESCE(run_issued_utc, dedupe_key) DESC LIMIT 32",
        (site_id, spec.id),
    ).fetchall()
    if not runs:
        return None, None

    latest = runs[0]
    selected = latest
    threshold = LONG_CYCLE_MIN_LEAD_HOURS.get(spec.id)
    reason = "latest"
    if threshold is not None:
        latest_dt = _date(latest["run_issued_utc"] or latest["dedupe_key"])
        for candidate in runs:
            issued = _date(candidate["run_issued_utc"])
            if not issued or issued.hour not in (0, 12):
                continue
            if latest_dt and latest_dt - issued > timedelta(hours=MAX_LONG_ISSUE_AGE_VS_LATEST_HOURS):
                continue
            _, lead = _coverage(conn, candidate)
            if lead is not None and lead >= threshold:
                selected = candidate
                reason = "recent-long-cycle-verified"
                break

    last_valid, lead = _coverage(conn, selected)
    issued = _date(selected["run_issued_utc"])
    if threshold is None:
        kind = "standard"
    elif not issued:
        kind = "unknown"
    elif issued.hour in (0, 12) and lead is not None and lead >= threshold:
        kind = "long"
    elif issued.hour in (0, 12):
        kind = "incomplete-long"
    else:
        kind = "short"

    info = {
        "cycle_kind": kind,
        "selection_reason": reason,
        "latest_archived_issue_utc": latest["run_issued_utc"],
        "selected_issue_utc": selected["run_issued_utc"],
        "last_usable_wind_utc": last_valid,
        "available_lead_hours": lead,
        "full_horizon_hours": spec.max_lead_hours,
        "newer_issue_preserved": selected["id"] != latest["id"],
    }
    return selected, info
