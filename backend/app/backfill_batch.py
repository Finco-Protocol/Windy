"""Bounded batch historical backfill.

Extends the v1 one-run-per-call backfill with a bounded batch workflow:

- The caller picks models, a UTC issue-date range (<= 31 days), run cycles
  (00/06/12/18) and a hard cap on provider requests (`max_requests`, <= 200).
- A dry-run preview plans the run list and estimates storage without any
  provider request.
- Execution happens on a background thread, one run at a time, with a SQLite
  checkpoint after every task: page reloads and process restarts lose nothing,
  and an interruption never corrupts the archive (each run is stored through
  the same immutable store_run as v1).
- Statuses per planned run: stored | already-archived | unavailable | failed |
  skipped (skipped = beyond the request cap, decided at planning time).
- Cancel is cooperative: the worker observes status `cancelling` between tasks
  and stops cleanly. Resume restarts the worker from the persisted cursor.
- Jobs left `running` by a process restart are marked `paused` at startup;
  they resume only through an explicit user action.

Archive integrity contracts preserved: real historical issues only (Single
Runs archive, original issue timestamps), never reanalysis; store_run is an
immutable no-op for existing issues; provider rate limits handled by the
client's retry/backoff.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import db as dbm
from .collect import iso_z
from .openmeteo import ModelRunUnavailable, OMError, OpenMeteoClient
from .registry import WINDY_MODELS, get_model

log = logging.getLogger("windy.backfill_batch")

MAX_RANGE_DAYS = 31
MAX_REQUESTS_CAP = 200
VALID_CYCLES = (0, 6, 12, 18)
# After this many consecutive network-level failures the job pauses itself
# instead of burning the remaining request budget against a dead provider.
CONSECUTIVE_FAIL_PAUSE = 5

JOB_STATUSES = ("running", "cancelling", "paused", "completed", "cancelled", "failed")


class BatchError(ValueError):
    """Invalid batch parameters (mapped to HTTP 400 by main.py)."""


@dataclass(frozen=True)
class BatchParams:
    models: tuple[str, ...]
    date_from: str  # YYYY-MM-DD
    date_to: str
    cycles: tuple[int, ...]
    days: int
    max_requests: int
    dry_run: bool = False


def parse_params(body: dict) -> BatchParams:
    models = body.get("models") or [m.id for m in WINDY_MODELS]
    if not isinstance(models, list) or not models:
        raise BatchError("models must be a non-empty list")
    for m in models:
        get_model(m)  # raises KeyError for unknown ids
    date_from = str(body.get("date_from") or "")
    date_to = str(body.get("date_to") or "")
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise BatchError("date_from and date_to must be YYYY-MM-DD") from exc
    if d_to < d_from:
        raise BatchError("date_to must not be before date_from")
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise BatchError(f"date range is limited to {MAX_RANGE_DAYS} days")
    if date(d_to.year, d_to.month, d_to.day) > datetime.now(timezone.utc).date():
        raise BatchError("date_to must not be in the future")

    cycles = body.get("cycles") or list(VALID_CYCLES)
    if not isinstance(cycles, list) or not cycles or any(c not in VALID_CYCLES for c in cycles):
        raise BatchError(f"cycles must be a non-empty subset of {list(VALID_CYCLES)}")

    days = int(body.get("days") or 10)
    if not 1 <= days <= 16:
        raise BatchError("days must be 1..16")
    max_requests = int(body.get("max_requests") or 60)
    if not 1 <= max_requests <= MAX_REQUESTS_CAP:
        raise BatchError(f"max_requests must be 1..{MAX_REQUESTS_CAP}")

    return BatchParams(
        models=tuple(models),
        date_from=date_from,
        date_to=date_to,
        cycles=tuple(sorted(set(int(c) for c in cycles))),
        days=days,
        max_requests=max_requests,
        dry_run=bool(body.get("dry_run")),
    )


def plan_runs(params: BatchParams) -> list[dict]:
    """Ordered planned runs: model-major, then date, then cycle. Runs beyond
    `max_requests` are kept in the plan but flagged `skipped` — the cap is a
    provider-request budget, so planning stays transparent about what was cut."""
    d_from = datetime.strptime(params.date_from, "%Y-%m-%d").date()
    d_to = datetime.strptime(params.date_to, "%Y-%m-%d").date()
    planned: list[dict] = []
    for model_id in params.models:
        spec = get_model(model_id)
        d = d_from
        while d <= d_to:
            for cycle in params.cycles:
                planned.append(
                    {
                        "model_id": model_id,
                        "run_iso": f"{d.isoformat()}T{cycle:02d}:00",
                        "request_days": min(params.days, spec.request_days),
                        "skipped": False,
                    }
                )
            d += timedelta(days=1)
    n_requests = 0
    for item in planned:
        if item["skipped"]:
            continue
        if n_requests >= params.max_requests:
            item["skipped"] = True
        else:
            n_requests += 1
    return planned


def plan_preview(params: BatchParams) -> dict:
    planned = plan_runs(params)
    per_model: dict[str, dict] = {}
    for item in planned:
        slot = per_model.setdefault(
            item["model_id"],
            {"model_id": item["model_id"], "planned": 0, "skipped": 0, "est_points": 0},
        )
        if item["skipped"]:
            slot["skipped"] += 1
        else:
            slot["planned"] += 1
            slot["est_points"] += item["request_days"] * 24
    return {
        "dry_run": True,
        "params": params.__dict__,
        "total_planned": sum(s["planned"] for s in per_model.values()),
        "total_skipped": sum(s["skipped"] for s in per_model.values()),
        "est_points": sum(s["est_points"] for s in per_model.values()),
        "per_model": list(per_model.values()),
        "note": (
            "Estimates assume full horizons; the provider's actual archive depth and "
            "per-run publication decide what is really stored."
        ),
    }


# --- job persistence -----------------------------------------------------------


def create_job(db_path, params: BatchParams) -> int:
    planned = plan_runs(params)
    now = iso_z(datetime.now(timezone.utc))
    with dbm.session(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO backfill_jobs (created_at_utc, params_json, planned_runs_json, status) VALUES (?, ?, ?, 'paused')",
            (now, dbm.j(params.__dict__), dbm.j(planned)),
        )
        job_id = int(cur.lastrowid)
    return job_id


def get_job(db_path, job_id: int) -> dict | None:
    with dbm.session(db_path) as conn:
        row = conn.execute("SELECT * FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["params"] = json.loads(job.pop("params_json"))
        planned_total = len(json.loads(job.pop("planned_runs_json")))
        results = conn.execute(
            "SELECT seq, model_id, run_issued_utc, status, detail FROM backfill_results WHERE job_id=? ORDER BY seq",
            (job_id,),
        ).fetchall()
    job["planned_total"] = planned_total
    job["results"] = [dict(r) for r in results]
    return job


def list_jobs(db_path, limit: int = 20) -> list[dict]:
    with dbm.session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, created_at_utc, status, cursor_index, n_stored, n_already, n_unavailable, "
            "n_failed, n_skipped, last_error, params_json FROM backfill_jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    jobs = []
    for r in rows:
        job = dict(r)
        job["params"] = json.loads(job.pop("params_json"))
        jobs.append(job)
    return jobs


# --- execution -----------------------------------------------------------------

# One worker thread per job; the registry only tracks live threads so resume
# never spawns a duplicate worker for the same job.
_workers: dict[int, threading.Thread] = {}


def _record_result(db_path, job_id: int, seq: int, item: dict, status: str, detail: str | None, counter: str) -> None:
    with dbm.session(db_path) as conn:
        conn.execute(
            "INSERT INTO backfill_results (job_id, seq, model_id, run_issued_utc, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, seq) DO UPDATE SET status=excluded.status, detail=excluded.detail",
            (job_id, seq, item["model_id"], item["run_iso"], status, detail),
        )
        conn.execute(
            f"UPDATE backfill_jobs SET {counter} = {counter} + 1, cursor_index = ? WHERE id = ?",
            (seq + 1, job_id),
        )


def _set_status(db_path, job_id: int, status: str, last_error: str | None = None) -> None:
    with dbm.session(db_path) as conn:
        if last_error is None:
            conn.execute("UPDATE backfill_jobs SET status=? WHERE id=?", (status, job_id))
        else:
            conn.execute("UPDATE backfill_jobs SET status=?, last_error=? WHERE id=?", (status, last_error, job_id))


def _job_status(db_path, job_id: int) -> str | None:
    with dbm.session(db_path) as conn:
        row = conn.execute("SELECT status FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()
    return row["status"] if row else None


def _cursor(db_path, job_id: int) -> int:
    with dbm.session(db_path) as conn:
        row = conn.execute("SELECT cursor_index FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()
    return int(row["cursor_index"]) if row else 0


def _worker(db_path, job_id: int, client: OpenMeteoClient, settings) -> None:
    """Run the planned tasks from the persisted cursor, checkpointing each one."""
    try:
        with dbm.session(db_path) as conn:
            row = conn.execute("SELECT planned_runs_json FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()
            planned = json.loads(row["planned_runs_json"])
        consecutive_failures = 0
        from .backfill import run_backfill_one

        for seq in range(len(planned)):
            item = planned[seq]
            if seq < _cursor(db_path, job_id):
                continue  # already checkpointed before an interruption
            status = _job_status(db_path, job_id)
            if status == "cancelling":
                _set_status(db_path, job_id, "cancelled")
                return
            if status != "running":
                return  # externally paused/removed

            if item.get("skipped"):
                _record_result(db_path, job_id, seq, item, "skipped", "beyond max_requests", "n_skipped")
                continue
            try:
                result = run_backfill_one(
                    settings,
                    item["model_id"],
                    item["run_iso"],
                    item["request_days"],
                    client=client,
                )
                counter = "n_stored" if result["created"] else "n_already"
                _record_result(db_path, job_id, seq, item, "stored" if result["created"] else "already-archived", None, counter)
                consecutive_failures = 0
            except ModelRunUnavailable as exc:
                # Provider says this cycle does not exist in the archive: an
                # expected, honest outcome — keep going with the rest.
                _record_result(db_path, job_id, seq, item, "unavailable", str(exc)[:300], "n_unavailable")
            except OMError as exc:
                _record_result(db_path, job_id, seq, item, "failed", str(exc)[:300], "n_failed")
                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAIL_PAUSE:
                    _set_status(db_path, job_id, "paused", f"paused after {consecutive_failures} consecutive failures: {exc}"[:500])
                    return
        _set_status(db_path, job_id, "completed")
    except Exception as exc:  # the worker must never crash the process
        log.exception("backfill job %s crashed", job_id)
        try:
            _set_status(db_path, job_id, "failed", str(exc)[:500])
        except Exception:
            pass
    finally:
        _workers.pop(job_id, None)


def start_job(db_path, job_id: int, client: OpenMeteoClient, settings) -> bool:
    """Spawn the worker thread unless one is already running for this job."""
    if job_id in _workers and _workers[job_id].is_alive():
        return False
    _set_status(db_path, job_id, "running")
    thread = threading.Thread(target=_worker, args=(db_path, job_id, client, settings), daemon=True, name=f"backfill-{job_id}")
    _workers[job_id] = thread
    thread.start()
    return True


def request_cancel(db_path, job_id: int) -> None:
    if _job_status(db_path, job_id) == "running":
        _set_status(db_path, job_id, "cancelling")


def mark_interrupted_jobs_paused(db_path) -> int:
    """At app startup: a `running` job means the process died mid-run. Its
    checkpoints are safe (per-task commits); mark it paused for explicit resume."""
    with dbm.session(db_path) as conn:
        cur = conn.execute(
            "UPDATE backfill_jobs SET status='paused', "
            "last_error=COALESCE(last_error, 'interrupted by process restart — resume explicitly') "
            "WHERE status IN ('running', 'cancelling')"
        )
        return cur.rowcount
