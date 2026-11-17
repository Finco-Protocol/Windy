"""Windy FastAPI application.

Small, single-user, local-first. All forecast data flows through the archive:
the Forecast screen reads the latest archived runs, so what you see is exactly
what was collected — never an ad-hoc re-fetch.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import accuracy as accuracy_mod
from . import availability as availability_mod
from . import calibration as calibration_mod
from . import db as dbm
from . import store
from .actuals import ActualsError, parse_actuals, persist_actuals
from .backfill_batch import (
    BatchError,
    create_job,
    get_job,
    list_jobs,
    mark_interrupted_jobs_paused,
    parse_params,
    plan_preview,
    request_cancel,
    start_job,
)
from .collect import collect_site, iso_z, parse_utc
from .config import Settings
from .openmeteo import OMError, OpenMeteoClient
from .registry import SOURCE_PROVIDER, WINDY_MODELS
from .scheduler import start_scheduler

log = logging.getLogger("windy")

# Upload guard for the single-user local app: generous for meter CSVs/XLSX,
# but bounded so a stray huge file cannot exhaust memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def create_app(settings: Settings | None = None, om_client: OpenMeteoClient | None = None) -> FastAPI:
    settings = settings or Settings()
    client = om_client or OpenMeteoClient(timeout_s=settings.om_timeout_s, max_retries=settings.om_max_retries)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure_db(settings.db_path)
        # Jobs left 'running' by a dead process are checkpoint-safe but must not
        # silently continue: mark them paused; resume is an explicit user action.
        n_interrupted = mark_interrupted_jobs_paused(settings.db_path)
        if n_interrupted:
            log.info("marked %d interrupted backfill job(s) as paused", n_interrupted)
        app.state.scheduler = start_scheduler(app, settings) if settings.scheduler_enabled else None
        log.info("Windy backend ready (db=%s, scheduler=%s)", settings.db_path, bool(app.state.scheduler))
        yield
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)

    app = FastAPI(title="Windy", version="1.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.om_client = client

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- error mapping ----------------------------------------------------
    # Handlers RETURN proper JSON responses (raising HTTPException from inside
    # an exception handler would escape the handler middleware and surface as
    # an opaque 500). Expected invalid input never becomes a 500.

    @app.exception_handler(ActualsError)
    async def _actuals_error(_req: Request, exc: ActualsError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def _key_error(_req: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc.args[0] if exc.args else exc)})

    @app.exception_handler(OMError)
    async def _om_error(_req: Request, exc: OMError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # BatchError subclasses ValueError; declared for clarity in OpenAPI errors.
    _ = BatchError

    # ---- basics ------------------------------------------------------------

    @app.get("/api/health")
    def health():
        with dbm.session(settings.db_path) as conn:
            version = dbm.get_meta(conn, "schema_version")
        return {
            "ok": True,
            "schema_version": version,
            "now_utc": iso_z(datetime.now(timezone.utc)),
            "db_path": str(settings.db_path),
            "scheduler_enabled": settings.scheduler_enabled,
        }

    @app.get("/api/assets")
    def assets_list():
        return {"assets": store.list_assets(settings.db_path)}

    @app.post("/api/assets")
    def assets_create(body: dict):
        return store.create_asset(settings.db_path, body or {})

    @app.get("/api/assets/{site_id}")
    def assets_get(site_id: str):
        return {"site": store.load_site(settings.db_path, site_id)}

    @app.get("/api/power-curves")
    def curves_list():
        return {"curves": store.list_curves(settings.db_path)}

    # ---- availability & curtailment ------------------------------------------------

    from .availability import AvailabilityError

    @app.exception_handler(AvailabilityError)
    async def _availability_error(_req: Request, exc: AvailabilityError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/assets/{site_id}/turbines")
    def turbines_get(site_id: str, revision: int | None = Query(None)):
        store.load_site(settings.db_path, site_id)
        return availability_mod.get_inventory(settings.db_path, site_id, revision)

    @app.post("/api/assets/{site_id}/turbines/generate")
    def turbines_generate(site_id: str, body: dict):
        store.load_site(settings.db_path, site_id)
        body = body or {}
        return availability_mod.generate_inventory(
            settings.db_path, site_id, body.get("count"), body.get("rating_mw"), str(body.get("unit_prefix") or "WTG")
        )

    @app.post("/api/assets/{site_id}/turbines/bulk-ratings")
    def turbines_bulk_ratings(site_id: str, body: dict):
        store.load_site(settings.db_path, site_id)
        return availability_mod.bulk_edit_ratings(settings.db_path, site_id, (body or {}).get("edits") or [])

    @app.delete("/api/assets/{site_id}/turbines")
    def turbines_delete(site_id: str):
        store.load_site(settings.db_path, site_id)
        return availability_mod.delete_inventory(settings.db_path, site_id)

    @app.get("/api/assets/{site_id}/constraints")
    def constraints_get(site_id: str, revision: int | None = Query(None)):
        store.load_site(settings.db_path, site_id)
        return availability_mod.get_constraints(settings.db_path, site_id, revision)

    @app.post("/api/assets/{site_id}/constraints")
    def constraints_add(site_id: str, body: dict):
        store.load_site(settings.db_path, site_id)
        return availability_mod.add_constraints(settings.db_path, site_id, (body or {}).get("rows") or [])

    @app.post("/api/assets/{site_id}/constraints/delete")
    def constraints_delete(site_id: str, body: dict):
        store.load_site(settings.db_path, site_id)
        return availability_mod.delete_constraints(settings.db_path, site_id, (body or {}).get("ids") or [])

    @app.get("/api/assets/{site_id}/status")
    def availability_status(site_id: str):
        store.load_site(settings.db_path, site_id)
        return availability_mod.site_status(settings.db_path, site_id)

    @app.get("/api/assets/{site_id}/scenario")
    def scenario_get(site_id: str, run_id: int, inventory_revision: int | None = Query(None), constraints_revision: int | None = Query(None)):
        store.load_site(settings.db_path, site_id)
        return availability_mod.scenario_for_run(settings.db_path, site_id, run_id, inventory_revision, constraints_revision)

    @app.get("/api/assets/{site_id}/scenario/export.csv")
    def scenario_export_csv(
        site_id: str,
        run_id: int,
        inventory_revision: int | None = Query(None),
        constraints_revision: int | None = Query(None),
    ):
        """Hourly scenario values for one pinned revision: baseline vs adjusted,
        loss split, revision ids and per-point status flags."""
        store.load_site(settings.db_path, site_id)
        sc = availability_mod.scenario_for_run(settings.db_path, site_id, run_id, inventory_revision, constraints_revision)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "site_id", "model_id", "run_issued_utc", "inventory_revision", "constraints_revision",
            "algorithm_version", "curve_mode", "baseline_comparable", "valid_at_utc", "interval_hours",
            "baseline_mw", "baseline_mwh", "scenario_base_mw", "scenario_base_mwh",
            "adjusted_mw", "adjusted_mwh", "pre_export_mw",
            "availability_loss_mwh", "export_loss_mwh", "status",
        ])
        for p in sc["points"]:
            ih = p["interval_hours"]
            baseline_mwh = round(p["base_mw"] * ih, 6) if p["base_mw"] is not None else None
            scenario_base_mwh = round(p["scenario_base_mw"] * ih, 6) if p["scenario_base_mw"] is not None else None
            adjusted_mwh = round(p["adjusted_mw"] * ih, 6) if p["adjusted_mw"] is not None else None
            w.writerow([
                site_id, sc["model_id"], sc["run_issued_utc"] or "", sc["inventory_revision"],
                sc["constraints_revision"], sc["algorithm_version"], sc["summary"]["curve_mode"],
                sc["baseline_comparable"], p["valid_at_utc"], ih,
                p["base_mw"], baseline_mwh, p["scenario_base_mw"], scenario_base_mwh,
                p["adjusted_mw"], adjusted_mwh, p["pre_export_mw"],
                p["lost_to_unit_mwh"], p["lost_to_export_mwh"], p["status"],
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=windy_scenario_{site_id}_run{run_id}.csv"},
        )

    @app.post("/api/assets/{site_id}/scenario/publish")
    def scenario_publish(site_id: str, body: dict):
        """Publish a scenario revision snapshot (summary pinned to immutable
        inputs). Republishing the same inputs is an idempotent no-op."""
        import json as _json

        store.load_site(settings.db_path, site_id)
        body = body or {}
        run_id = int(body.get("run_id"))
        inv_rev = body.get("inventory_revision")
        cons_rev = body.get("constraints_revision")
        scenario = availability_mod.scenario_for_run(settings.db_path, site_id, run_id, inv_rev, cons_rev)
        summary = {
            "installed_mw": scenario["installed_mw"],
            "n_turbines": scenario["n_turbines"],
            "curve_mode": scenario["summary"]["curve_mode"],
            "windows": scenario["summary"]["windows"],
            "note": scenario["note"],
        }
        now = iso_z(datetime.now(timezone.utc))
        with dbm.session(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO scenario_revisions (site_id, model_id, run_id, inventory_revision, "
                "constraints_revision, algorithm_version, created_at_utc, summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(site_id, run_id, inventory_revision, constraints_revision) DO NOTHING",
                (
                    site_id,
                    scenario["model_id"],
                    run_id,
                    scenario["inventory_revision"],
                    scenario["constraints_revision"],
                    availability_mod.ALGORITHM_VERSION,
                    now,
                    dbm.j(summary),
                ),
            )
            row = conn.execute(
                "SELECT id, created_at_utc, summary_json FROM scenario_revisions "
                "WHERE site_id=? AND run_id=? AND inventory_revision=? AND constraints_revision=?",
                (site_id, run_id, scenario["inventory_revision"], scenario["constraints_revision"]),
            ).fetchone()
        return {
            "scenario_revision_id": row["id"],
            "created_at_utc": row["created_at_utc"],
            "summary": json.loads(row["summary_json"]),
            "points_available_at": (
                f"/api/assets/{site_id}/scenario?run_id={run_id}"
                f"&inventory_revision={scenario['inventory_revision']}&constraints_revision={scenario['constraints_revision']}"
            ),
            "retrieval": {
                "run_id": run_id,
                "site_id": site_id,
                "inventory_revision": scenario["inventory_revision"],
                "constraints_revision": scenario["constraints_revision"],
                "note": "pinned to the exact revisions this snapshot was computed from — reopening reproduces THIS revision, not the currently active configuration",
            },
        }

    @app.get("/api/assets/{site_id}/scenario/revisions")
    def scenario_revisions_list(site_id: str):
        store.load_site(settings.db_path, site_id)
        with dbm.session(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT sr.id AS id, sr.site_id AS site_id, sr.model_id AS model_id, sr.run_id AS run_id, "
                "fr.run_issued_utc AS run_issued_utc, sr.inventory_revision AS inventory_revision, "
                "sr.constraints_revision AS constraints_revision, sr.algorithm_version AS algorithm_version, "
                "sr.created_at_utc AS created_at_utc, sr.summary_json AS summary_json "
                "FROM scenario_revisions sr LEFT JOIN forecast_runs fr ON fr.id = sr.run_id "
                "WHERE sr.site_id=? ORDER BY sr.id DESC LIMIT 100",
                (site_id,),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            item["retrieve_at"] = (
                f"/api/assets/{site_id}/scenario?run_id={item['run_id']}"
                f"&inventory_revision={item['inventory_revision']}&constraints_revision={item['constraints_revision']}"
            )
            out.append(item)
        return {"revisions": out}

    @app.get("/api/site")
    def site(site_id: str = Query("krnovo")):
        site_row = store.load_site(settings.db_path, site_id)
        curve = store.load_curve(settings.db_path, site_row.get("power_curve_id"))
        return {
            "site": site_row,
            "power_curve": {
                "id": curve.id,
                "label": curve.label,
                "status": curve.status,
                "source": curve.source,
                "provenance": curve.provenance,
                "rated_power_mw": curve.rated_power_mw,
                "cut_in_mps": curve.cut_in_mps,
                "rated_mps": curve.rated_mps,
                "cut_out_mps": curve.cut_out_mps,
            },
            "models": [
                {
                    "id": m.id,
                    "provider": m.provider,
                    "label": m.label,
                    "max_lead_hours": m.max_lead_hours,
                    "request_days": m.request_days,
                    "height_method": m.height_method,
                    "speed_heights_m": list(m.speed_heights),
                    "note": m.note,
                }
                for m in WINDY_MODELS
            ],
            "source_provider": SOURCE_PROVIDER,
            "source_note": "All models are fetched via Open-Meteo; ECMWF/NOAA/DWD are the underlying model origins, not separate providers.",
        }

    # ---- collection ---------------------------------------------------------

    @app.post("/api/collect")
    def collect(body: dict | None = None):
        body = body or {}
        site_id = str(body.get("site_id") or "krnovo")
        store.load_site(settings.db_path, site_id)  # 404 for unknown sites
        days = int(body.get("days") or settings.collect_days)
        if not 1 <= days <= 16:
            raise ValueError("days must be 1..16")
        return collect_site(settings.db_path, client, site_id, days=days)

    @app.post("/api/backfill")
    def backfill(body: dict):
        """Bounded, opt-in historical backfill: ONE archived run per call."""
        from .backfill import ModelRunUnavailable as MRU
        from .backfill import run_backfill

        try:
            return run_backfill(
                settings,
                body.get("model_id"),
                str(body.get("run_date") or ""),
                int(body.get("run_hour", 0)),
                int(body.get("days") or 10),
                client=client,
                site_id=str(body.get("site_id") or "krnovo"),
            )
        except MRU as exc:
            raise HTTPException(status_code=404, detail=f"UNAVAILABLE: {exc}") from exc

    # ---- batch historical backfill -------------------------------------------

    @app.post("/api/backfill/batch")
    def backfill_batch(body: dict):
        """Dry-run preview or start of a bounded batch backfill job."""
        params = parse_params(body or {})
        if params.dry_run:
            return plan_preview(params)
        job_id = create_job(settings.db_path, params)
        start_job(settings.db_path, job_id, client, settings)
        return {"job_id": job_id, "status": "running"}

    @app.get("/api/backfill/batch")
    def backfill_batch_list(site_id: str | None = Query(None)):
        return {"jobs": list_jobs(settings.db_path, site_id=site_id)}

    @app.get("/api/backfill/batch/{job_id}")
    def backfill_batch_get(job_id: int):
        job = get_job(settings.db_path, job_id)
        if job is None:
            raise KeyError(f"no backfill job {job_id}")
        return job

    @app.post("/api/backfill/batch/{job_id}/cancel")
    def backfill_batch_cancel(job_id: int):
        if get_job(settings.db_path, job_id) is None:
            raise KeyError(f"no backfill job {job_id}")
        request_cancel(settings.db_path, job_id)
        return {"job_id": job_id, "status": get_job(settings.db_path, job_id)["status"]}

    @app.post("/api/backfill/batch/{job_id}/resume")
    def backfill_batch_resume(job_id: int):
        job = get_job(settings.db_path, job_id)
        if job is None:
            raise KeyError(f"no backfill job {job_id}")
        if job["status"] not in ("paused", "cancelled", "failed"):
            raise ValueError(f"job {job_id} is '{job['status']}' — only paused/cancelled/failed jobs can resume")
        start_job(settings.db_path, job_id, client, settings)
        return {"job_id": job_id, "status": "running"}

    # ---- archive health ---------------------------------------------------------

    @app.get("/api/archive/health")
    def archive_health(site_id: str = Query("krnovo")):
        """Coverage per model for one site: verified issue cycles vs unknown-issue
        live fallbacks, first/last issue, points, missing 6-hourly cycles, last
        fetch and last recorded error."""
        store.load_site(settings.db_path, site_id)
        out = []
        with dbm.session(settings.db_path) as conn:
            points_per_model = {
                r["model_id"]: r["n"]
                for r in conn.execute(
                    "SELECT fr.model_id AS model_id, COUNT(*) AS n FROM forecast_points fp "
                    "JOIN forecast_runs fr ON fr.id = fp.run_id WHERE fr.site_id=? AND fr.status='ok' "
                    "GROUP BY fr.model_id",
                    (site_id,),
                ).fetchall()
            }
            for spec in WINDY_MODELS:
                runs = conn.execute(
                    "SELECT run_issued_utc, id FROM forecast_runs "
                    "WHERE site_id=? AND model_id=? AND status='ok' ORDER BY run_issued_utc",
                    (site_id, spec.id),
                ).fetchall()
                last_row = conn.execute(
                    "SELECT fetched_at_utc, error FROM forecast_runs WHERE site_id=? AND model_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (site_id, spec.id),
                ).fetchone()
                known = [r for r in runs if r["run_issued_utc"] is not None]
                n_points = points_per_model.get(spec.id, 0)
                entry = {
                    "model_id": spec.id,
                    "label": spec.label,
                    "known_issue_cycles": len(set(r["run_issued_utc"] for r in known)),
                    "issue_unknown_runs": len(runs) - len(known),
                    "first_issue_utc": known[0]["run_issued_utc"] if known else None,
                    "last_issue_utc": known[-1]["run_issued_utc"] if known else None,
                    "last_fetched_utc": last_row["fetched_at_utc"] if last_row else None,
                    "last_error": last_row["error"] if last_row else None,
                    "total_points": n_points,
                    "missing_cycles": [],
                    "n_missing_cycles": None,
                    "note": "issue-unknown runs (live fallback) are not scoreable and never counted as verified history",
                }
                if known:
                    expected = set()
                    cur = parse_utc(known[0]["run_issued_utc"])
                    last = parse_utc(known[-1]["run_issued_utc"])
                    while cur <= last:
                        expected.add(cur.strftime("%Y-%m-%dT%H:%M:%SZ"))
                        cur += timedelta(hours=6)
                    have = set(r["run_issued_utc"] for r in known)
                    missing = sorted(expected - have)
                    entry["n_missing_cycles"] = len(missing)
                    entry["missing_cycles"] = missing[:12]
                out.append(entry)
        return {"models": out}

    # ---- calibration (optional, derived series only) ------------------------------

    @app.get("/api/calibration")
    def calibration_get(site_id: str = Query("krnovo")):
        return {
            "site_id": site_id,
            "models": calibration_mod.get_calibrations(settings.db_path, site_id),
            "note": (
                "Optional explainable calibration (bounded per-model multiplicative factor, "
                "latest-issue-per-hour selection policy). Archived forecast values are never "
                "modified; factors apply to derived views only. Retrospective analysis — not "
                "what was knowable at issuance."
            ),
        }

    @app.post("/api/calibration/train")
    def calibration_train(body: dict):
        site_id = str((body or {}).get("site_id") or "krnovo")
        from_utc = str((body or {}).get("from_utc") or "")
        to_utc = str((body or {}).get("to_utc") or "")
        if not from_utc or not to_utc:
            raise ValueError("from_utc and to_utc are required")
        return calibration_mod.train(settings.db_path, site_id, from_utc, to_utc)

    # ---- forecast display ----------------------------------------------------

    @app.get("/api/forecast")
    def forecast(days: int = Query(10, ge=1, le=16), site_id: str = Query("krnovo")):
        site_row = store.load_site(settings.db_path, site_id)
        curve = store.load_curve(settings.db_path, site_row.get("power_curve_id"))
        tz = ZoneInfo(site_row["timezone"])
        horizon_end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        models_out = []
        with dbm.session(settings.db_path) as conn:
            for spec in WINDY_MODELS:
                run = conn.execute(
                    """
                    SELECT * FROM forecast_runs
                    WHERE site_id=? AND model_id=? AND status='ok'
                    ORDER BY COALESCE(run_issued_utc, dedupe_key) DESC LIMIT 1
                    """,
                    (site_row["id"], spec.id),
                ).fetchone()
                entry: dict = {
                    "model_id": spec.id,
                    "provider": spec.provider,
                    "label": spec.label,
                    "height_method": spec.height_method,
                    "height_sources_m": list(spec.speed_heights),
                    "requested_height_m": float(site_row["hub_height_m"]),
                    "height_note": spec.note,
                    "run": None,
                    "points": [],
                    "daily_local": [],
                    "total_mwh_own_window": None,
                    "warnings": [],
                }
                if run is None:
                    entry["warnings"].append("No archived run yet — use 'Fetch now'.")
                    models_out.append(entry)
                    continue
                pts = conn.execute(
                    "SELECT * FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc",
                    (run["id"],),
                ).fetchall()
                visible = [p for p in pts if p["valid_at_utc"] <= horizon_end]
                rows = [dict(p) for p in visible]
                entry["run"] = {
                    "id": run["id"],
                    "run_issued_utc": run["run_issued_utc"],
                    "issue_source": run["issue_source"],
                    "fetched_at_utc": run["fetched_at_utc"],
                    "forecast_days": run["forecast_days"],
                    "source_url": run["source_url"],
                }
                if run["issue_source"] == "live-forecast-issue-unknown":
                    entry["warnings"].append("Issue time unknown (live-forecast fallback; archive had no run at collection time).")
                if run["run_issued_utc"] is None:
                    entry["warnings"].append("Historical comparison is not possible for this run (issue time unknown).")

                # daily totals in the site's local timezone; coverage-aware
                # Per-local-date aggregation in the SITE's IANA timezone with
                # real day spans (23/24/25 h across DST). Each archived point
                # owns the half-open UTC interval [valid_at, valid_at +
                # interval_hours); its energy is allocated proportionately to
                # every local day the interval overlaps (an interval crossing
                # local midnight contributes to both days). Coverage per day is
                # the merged union of valid intervals — duplicates never count
                # twice; missing energy hours reduce coverage and flag the day
                # partial. Metering (actuals) keeps its DST-aware handling.
                daily: dict[str, dict] = {}
                cum = 0.0
                counted_any = False

                def _day_span_utc(local_date: str) -> tuple[datetime, datetime, float]:
                    d = datetime.strptime(local_date, "%Y-%m-%d")

                    def midnight(day: datetime) -> datetime:
                        return day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz, fold=0)

                    # subtract in UTC: aware datetimes sharing the same tzinfo
                    # object do WALL-CLOCK arithmetic, which would report 24 h
                    # for a 23 h spring day
                    start_utc = midnight(d).astimezone(timezone.utc)
                    end_utc = midnight(d + timedelta(days=1)).astimezone(timezone.utc)
                    return start_utc, end_utc, (end_utc - start_utc).total_seconds() / 3600.0

                for r in rows:
                    if r["interval_energy_mwh"] is not None:
                        cum += r["interval_energy_mwh"]
                        counted_any = True
                    st = parse_utc(r["valid_at_utc"])
                    dur = float(r["interval_hours"] or 1.0)
                    en = st + timedelta(hours=dur)
                    energy = r["interval_energy_mwh"]
                    remaining = dur
                    cursor = st
                    # allocate the interval across local day(s) it overlaps
                    while remaining > 1e-9:
                        day_key = cursor.astimezone(tz).strftime("%Y-%m-%d")
                        d_start, d_end, d_hours = _day_span_utc(day_key)
                        seg_end = min(cursor + timedelta(hours=remaining), d_end)
                        seg_start = max(cursor, d_start)
                        overlap = (seg_end - seg_start).total_seconds() / 3600.0
                        if overlap <= 0:
                            cursor = d_end
                            continue
                        slot = daily.setdefault(
                            day_key,
                            {
                                "date": day_key,
                                "mwh": 0.0,
                                "hours_counted": 0.0,
                                "hours_missing": 0.0,
                                "hours_expected": round(d_hours, 3),
                                "partial": False,
                                "_intervals": [],
                            },
                        )
                        if energy is not None:
                            slot["mwh"] = round(slot["mwh"] + energy * (overlap / dur), 4)
                            slot["hours_counted"] = round(slot["hours_counted"] + overlap, 6)
                            slot["_intervals"].append((seg_start, seg_end))
                        else:
                            slot["hours_missing"] = round(slot["hours_missing"] + overlap, 6)
                        remaining -= overlap
                        cursor = seg_end

                for slot in daily.values():
                    intervals = slot.pop("_intervals", [])
                    merged: list = []
                    for st_i, en_i in sorted(intervals):
                        if merged and st_i <= merged[-1][1] + timedelta(seconds=1):
                            merged[-1] = (merged[-1][0], max(merged[-1][1], en_i))
                        else:
                            merged.append((st_i, en_i))
                    union_h = round(sum((e - s).total_seconds() / 3600.0 for s, e in merged), 3)
                    slot["hours_counted"] = union_h
                    slot["hours_missing"] = round(max(slot["hours_expected"] - union_h, 0.0), 3)
                    slot["partial"] = union_h < slot["hours_expected"] - 1e-6
                entry["points"] = rows
                entry["daily_local"] = list(daily.values())
                entry["total_mwh_own_window"] = round(cum, 3) if counted_any else None
                entry["coverage"] = {
                    "n_hours": len(rows),
                    "first_valid_utc": rows[0]["valid_at_utc"] if rows else None,
                    "last_valid_utc": rows[-1]["valid_at_utc"] if rows else None,
                    "max_available_hours": spec.max_lead_hours,
                }
                if rows and rows[-1]["valid_at_utc"] < horizon_end:
                    entry["warnings"].append(
                        f"Model horizon ends at {rows[-1]['valid_at_utc']} — no data beyond (not extended)."
                    )
                models_out.append(entry)

        # Same-valid-time intersection totals (honest cross-model comparison)
        by_model_hours = {m["model_id"]: {p["valid_at_utc"] for p in m["points"]} for m in models_out}
        common = set.intersection(*by_model_hours.values()) if by_model_hours and all(by_model_hours.values()) else set()
        totals: dict[str, float] = {}
        for m in models_out:
            totals[m["model_id"]] = round(
                sum(p["interval_energy_mwh"] for p in m["points"] if p["valid_at_utc"] in common and p["interval_energy_mwh"] is not None),
                3,
            )
        intersection = {
            "n_common_hours": len(common),
            "first_common_utc": min(common) if common else None,
            "last_common_utc": max(common) if common else None,
            "total_mwh": totals,
            "note": "Totals over the common valid-time intersection only — models with shorter horizons are not padded.",
        }

        return {
            "site": site_row,
            "power_curve": {
                "id": curve.id,
                "label": curve.label,
                "status": curve.status,
                "provenance": curve.provenance,
            },
            "generated_at_utc": iso_z(datetime.now(timezone.utc)),
            "days": days,
            "models": models_out,
            "intersection": intersection,
        }

    # ---- runs / history --------------------------------------------------------

    @app.get("/api/runs")
    def runs(model_id: str | None = None, limit: int = Query(200, ge=1, le=1000), site_id: str = Query("krnovo")):
        q = """
            SELECT fr.id, fr.model_id, fr.run_issued_utc, fr.dedupe_key, fr.issue_source,
                   fr.fetched_at_utc, fr.forecast_days, fr.status, fr.requested_height_m,
                   (SELECT COUNT(*) FROM forecast_points p WHERE p.run_id=fr.id) AS n_points,
                   (SELECT MIN(valid_at_utc) FROM forecast_points p WHERE p.run_id=fr.id) AS first_valid_utc,
                   (SELECT MAX(valid_at_utc) FROM forecast_points p WHERE p.run_id=fr.id) AS last_valid_utc
            FROM forecast_runs fr WHERE fr.site_id=?
        """
        args: list = [site_id]
        if model_id:
            q += " AND fr.model_id=?"
            args.append(model_id)
        q += " ORDER BY COALESCE(fr.run_issued_utc, fr.dedupe_key) DESC LIMIT ?"
        args.append(limit)
        with dbm.session(settings.db_path) as conn:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        return {"runs": rows}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int, site_id: str = Query("krnovo")):
        with dbm.session(settings.db_path) as conn:
            run = conn.execute("SELECT * FROM forecast_runs WHERE id=?", (run_id,)).fetchone()
            if run is not None and run["site_id"] != site_id:
                run = None  # strict site isolation: another site's run does not exist for this caller
            if run is None:
                raise KeyError(f"no run {run_id}")
            points = [dict(p) for p in conn.execute(
                "SELECT * FROM forecast_points WHERE run_id=? ORDER BY valid_at_utc", (run_id,)
            ).fetchall()]
        out = dict(run)
        out.pop("raw_json", None)
        out["points"] = points
        return out

    @app.get("/api/compare")
    def compare(run_id: int, target_date: str, site_id: str = Query("krnovo")):
        """What did this archived issue predict for a later target date (UTC)."""
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("target_date must be YYYY-MM-DD") from exc
        with dbm.session(settings.db_path) as conn:
            run = conn.execute("SELECT * FROM forecast_runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["site_id"] != site_id:
                raise KeyError(f"no run {run_id}")
            points = [dict(p) for p in conn.execute(
                "SELECT * FROM forecast_points WHERE run_id=? AND valid_at_utc LIKE ? ORDER BY valid_at_utc",
                (run_id, f"{target_date}%"),
            ).fetchall()]
        total = round(sum(p["interval_energy_mwh"] or 0.0 for p in points), 3)
        return {
            "run_id": run_id,
            "model_id": run["model_id"],
            "run_issued_utc": run["run_issued_utc"],
            "target_date": target_date,
            "points": points,
            "total_mwh": total,
        }

    # ---- actuals -----------------------------------------------------------------

    def _read_upload(file: UploadFile) -> tuple[str, bytes]:
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise ActualsError("uploaded file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return file.filename or "upload.csv", data

    @app.post("/api/actuals/preview")
    async def actuals_preview(file: UploadFile = File(...), mapping: str = Form(...), site_id: str = Form("krnovo")):
        store.load_site(settings.db_path, site_id)  # 404 for unknown sites
        filename, data = _read_upload(file)
        mapping_dict = json.loads(mapping)
        parsed = parse_actuals(filename, data, mapping_dict)
        preview = {
            "site_id": site_id,
            "fieldnames": parsed.fieldnames,
            "rows_total": parsed.rows_total,
            "rows_parsed": len(parsed.rows),
            "interval_hours": parsed.interval_hours,
            "errors": parsed.errors[:200],
            "warnings": parsed.warnings[:200],
            "rows": [r.__dict__ for r in parsed.rows[:500]],
            "truncated": len(parsed.rows) > 500,
        }
        if parsed.rows:
            from .actuals import check_conflicts

            existing = check_conflicts(settings.db_path, site_id, parsed.rows)
            preview["n_conflicts_with_db"] = len(existing)
            preview["conflicts"] = existing[:100]
        return preview

    @app.post("/api/actuals/import")
    async def actuals_import(
        file: UploadFile = File(...),
        mapping: str = Form(...),
        on_conflict: str = Form("reject"),
        site_id: str = Form("krnovo"),
    ):
        store.load_site(settings.db_path, site_id)  # 404 for unknown sites
        filename, data = _read_upload(file)
        mapping_dict = json.loads(mapping)
        parsed = parse_actuals(filename, data, mapping_dict)
        # physical limit: readings implying more than capacity × 1.05 are
        # rejected at import (over-limit hours would never score anyway)
        site_row = store.load_site(settings.db_path, site_id)
        capacity = float(site_row["capacity_mw"]) * 1.05
        over = [
            {"interval_start_utc": r.interval_start_utc, "power_mw": r.power_mw}
            for r in parsed.rows
            if r.power_mw is not None and r.power_mw > capacity
        ]
        if over:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": (
                        f"{len(over)} reading(s) exceed the site physical limit ({capacity:.2f} MW = "
                        "capacity × 1.05) — import blocked."
                    ),
                    "readings": over[:50],
                },
            )
        if parsed.errors:
            # A file with invalid rows is blocked, never partially imported:
            # quiet partial imports would make the archive silently inconsistent.
            # An explicit partial-import opt-in may come in a future PR.
            raise HTTPException(
                status_code=400,
                detail={
                    "message": (
                        f"{len(parsed.errors)} row(s) failed validation — import blocked. "
                        "Fix the rows listed below (see /api/actuals/preview) and re-import."
                    ),
                    "n_errors": len(parsed.errors),
                    "errors": parsed.errors[:50],
                },
            )
        result = persist_actuals(settings.db_path, site_id, filename, mapping_dict, parsed, on_conflict=on_conflict)
        return result

    @app.get("/api/actuals")
    def actuals_list(
        from_utc: str = Query("1970-01-01T00:00:00Z"),
        to_utc: str = Query("2999-01-01T00:00:00Z"),
        limit: int = Query(2000, ge=1, le=50000),
        site_id: str = Query("krnovo"),
    ):
        f = accuracy_mod.parse_z(from_utc)
        t = accuracy_mod.parse_z(to_utc)
        with dbm.session(settings.db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM actual_points WHERE site_id=? AND interval_start_utc>=? AND interval_start_utc<? ORDER BY interval_start_utc LIMIT ?",
                (site_id, f, t, limit),
            ).fetchall()]
        return {"rows": rows}

    @app.get("/api/actuals/imports")
    def actuals_imports(site_id: str = Query("krnovo")):
        with dbm.session(settings.db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, filename, imported_at_utc, timezone, rows_total, rows_ok, warnings_json FROM actual_imports "
                "WHERE site_id=? ORDER BY id DESC LIMIT 100",
                (site_id,),
            ).fetchall()]
        for r in rows:
            r["warnings"] = json.loads(r.pop("warnings_json") or "[]")
            r["mapping"] = None
        return {"imports": rows}

    # ---- accuracy -----------------------------------------------------------------

    @app.get("/api/accuracy")
    def accuracy(from_utc: str, to_utc: str, use_calibration: bool = Query(False), site_id: str = Query("krnovo")):
        return accuracy_mod.accuracy(settings.db_path, site_id, from_utc, to_utc, use_calibration=use_calibration)

    @app.get("/api/accuracy/overlay")
    def accuracy_overlay(
        from_utc: str,
        to_utc: str,
        lead_bin: str | None = None,
        use_calibration: bool = Query(False),
        site_id: str = Query("krnovo"),
    ):
        return accuracy_mod.overlay(settings.db_path, site_id, from_utc, to_utc, lead_bin, use_calibration=use_calibration)

    # ---- export ---------------------------------------------------------------------

    @app.get("/api/export/forecast.csv")
    def export_forecast(
        run_id: int | None = None,
        model_id: str | None = None,
        from_utc: str | None = None,
        to_utc: str | None = None,
        site_id: str = Query("krnovo"),
    ):
        q = """
            SELECT fr.model_id, fr.run_issued_utc, fr.issue_source, fr.fetched_at_utc,
                   fr.source_url, fr.requested_height_m, fr.height_method,
                   fp.valid_at_utc, fp.wind_speed_mps, fp.wind_direction_deg,
                   fp.modeled_power_mw, fp.interval_energy_mwh, fp.interval_hours, fp.status
            FROM forecast_points fp JOIN forecast_runs fr ON fr.id = fp.run_id
            WHERE fr.site_id = ?
        """
        args: list = [site_id]
        if run_id:
            q += " AND fr.id=?"
            args.append(run_id)
        if model_id:
            q += " AND fr.model_id=?"
            args.append(model_id)
        if from_utc:
            q += " AND fp.valid_at_utc>=?"
            args.append(accuracy_mod.parse_z(from_utc))
        if to_utc:
            q += " AND fp.valid_at_utc<?"
            args.append(accuracy_mod.parse_z(to_utc))
        q += " ORDER BY fr.run_issued_utc, fr.model_id, fp.valid_at_utc"
        with dbm.session(settings.db_path) as conn:
            rows = conn.execute(q, args).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "model_id", "run_issued_utc", "issue_source", "fetched_at_utc", "source_url",
            "requested_height_m", "height_method", "valid_at_utc", "wind_speed_mps",
            "wind_direction_deg", "modeled_power_mw", "interval_energy_mwh",
            "interval_hours", "status",
        ])
        for r in rows:
            w.writerow([r[c] for c in (
                "model_id", "run_issued_utc", "issue_source", "fetched_at_utc", "source_url",
                "requested_height_m", "height_method", "valid_at_utc", "wind_speed_mps",
                "wind_direction_deg", "modeled_power_mw", "interval_energy_mwh",
                "interval_hours", "status",
            )])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=windy_forecast_archive.csv"},
        )

    @app.get("/api/export/actuals.csv")
    def export_actuals(site_id: str = Query("krnovo")):
        with dbm.session(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT interval_start_utc, interval_end_utc, energy_mwh, power_mw, import_id FROM actual_points "
                "WHERE site_id=? ORDER BY interval_start_utc",
                (site_id,),
            ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["interval_start_utc", "interval_end_utc", "energy_mwh", "power_mw", "import_id"])
        for r in rows:
            w.writerow([r["interval_start_utc"], r["interval_end_utc"], r["energy_mwh"], r["power_mw"], r["import_id"]])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=windy_actuals.csv"},
        )

    # ---- built frontend (Docker image) ----------------------------------------------
    #
    # The Vue app uses history-mode routes (/history, /actuals), so hard
    # refreshes and direct navigation must serve index.html. Starlette's
    # StaticFiles(html=True) does NOT do that for unknown deep links, so:
    #   - /assets/* is served statically (missing chunks stay real 404s),
    #   - a catch-all GET route serves existing root files (favicon, sw.js, ...)
    #     and falls back to index.html for application routes only.
    # API routes are registered before the catch-all, and unmatched /api/*
    # paths return JSON 404 — never index.html disguised as an API response.

    dist = settings.frontend_dist
    if dist and dist.exists():
        if (dist / "assets").is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        index_html = dist / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "not found"})
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist.resolve())  # traversal guard
            except ValueError:
                return JSONResponse(status_code=404, content={"detail": "not found"})
            if candidate.is_file():
                return FileResponse(candidate)
            if Path(full_path).suffix:  # a missing file with an extension stays a 404
                return JSONResponse(status_code=404, content={"detail": "not found"})
            if index_html.is_file():
                return FileResponse(index_html)
            return JSONResponse(status_code=404, content={"detail": "frontend build missing index.html"})

    return app


app = create_app()
