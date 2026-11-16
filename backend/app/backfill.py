"""Bounded, opt-in historical backfill of ONE archived forecast run.

Used by both the API (`POST /api/backfill`) and the CLI
(`python -m app.cli backfill`). One call = one run: deliberately bounded so a
user backfills deliberately, run by run, instead of hammering the archive.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db as dbm
from .collect import build_points, iso_z, normalize_run_iso, store_run
from .openmeteo import ModelRunUnavailable, OpenMeteoClient
from .registry import get_model
from .store import load_curve, load_site


def run_backfill_one(
    settings,
    model_id: str,
    run_iso: str,
    days: int = 10,
    client: OpenMeteoClient | None = None,
) -> dict:
    """Fetch and archive ONE run identified by its UTC issue stamp
    (`YYYY-MM-DDTHH:00`). Shared by the single-run API/CLI path and the batch
    workflow; preserves every v1 immutability guarantee."""
    date_part, _, hour_part = run_iso.partition("T")
    try:
        run_hour = int((hour_part or "00")[:2])
    except ValueError as exc:
        raise ValueError(f"invalid run stamp: {run_iso!r}") from exc
    spec = get_model(model_id)
    if run_hour not in (0, 6, 12, 18):
        raise ValueError("run_hour must be 0, 6, 12 or 18 (UTC run cycles)")
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("run_date must be YYYY-MM-DD") from exc
    days = max(1, min(int(days), spec.request_days))
    run_issued = normalize_run_iso(run_iso)

    site_row = load_site(settings.db_path, "krnovo")
    curve = load_curve(settings.db_path, site_row.get("power_curve_id"))
    client = client or OpenMeteoClient(timeout_s=settings.om_timeout_s, max_retries=settings.om_max_retries)

    raw = client.fetch_single_run(site_row["latitude"], site_row["longitude"], spec, run_iso, days)

    now = datetime.now(timezone.utc)
    points = build_points(
        raw,
        spec,
        float(site_row["hub_height_m"]),
        curve,
        int(site_row["turbine_count"]),
        float(site_row["loss_factor"]),
        float(site_row["capacity_mw"]),
    )
    with dbm.session(settings.db_path) as conn:
        run_id, created, stored_points = store_run(
            conn,
            site_row,
            spec,
            run_issued_utc=run_issued,
            dedupe_key=run_issued,
            issue_source="single-runs-archive",
            fetched_at_utc=iso_z(now),
            source_url=f"single-runs-api.open-meteo.com (run={run_iso}, model={spec.id}, backfill)",
            forecast_days=days,
            points=points,
            raw_json=dbm.j(raw),
        )
        coverage = None
        if not created:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(valid_at_utc) AS first_valid, MAX(valid_at_utc) AS last_valid "
                "FROM forecast_points WHERE run_id=?",
                (run_id,),
            ).fetchone()
            coverage = {
                "stored_points": int(row["n"]),
                "first_valid_utc": row["first_valid"],
                "last_valid_utc": row["last_valid"],
            }
    result = {
        "status": "ok",
        "run_id": run_id,
        "created": created,
        "model_id": spec.id,
        "run_issued_utc": run_issued,
        "points": stored_points,
        "note": (
            "Archived forecast issue stored unchanged. This is the model's own past "
            "prediction — never a reanalysis or a current-forecast substitute."
        ),
    }
    if not created:
        result["already_archived"] = True
        result["stored_coverage"] = coverage
        result["note"] = (
            "already-archived: this issue was stored earlier and its snapshot is immutable — "
            "the stored coverage is reported; nothing was extended or rewritten."
        )
    return result


def run_backfill(
    settings,
    model_id: str,
    run_date: str,
    run_hour: int,
    days: int = 10,
    client: OpenMeteoClient | None = None,
) -> dict:
    if not model_id or not run_date:
        raise ValueError("model_id and run_date (YYYY-MM-DD) are required")
    if run_hour not in (0, 6, 12, 18):
        raise ValueError("run_hour must be 0, 6, 12 or 18 (UTC run cycles)")
    return run_backfill_one(settings, model_id, f"{run_date}T{run_hour:02d}:00", days, client=client)


__all__ = ["run_backfill", "run_backfill_one", "ModelRunUnavailable"]
