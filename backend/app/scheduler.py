"""Optional twice-daily collection scheduler (00:00 / 12:00 UTC by default).

Enabled with WINDY_SCHEDULER_ENABLED=1. The same collection is also available
as `python -m app.cli collect` for cron/systemd deployments.
"""

from __future__ import annotations

import logging
from datetime import timezone

log = logging.getLogger("windy.scheduler")


def scheduled_collect(app) -> None:
    """One scheduled collection pass over all models for the default site."""
    from .collect import collect_site
    from .openmeteo import OpenMeteoClient

    settings = app.state.settings
    client = app.state.om_client or OpenMeteoClient(timeout_s=settings.om_timeout_s, max_retries=settings.om_max_retries)
    try:
        result = collect_site(settings.db_path, client, "krnovo")
        for m in result["models"]:
            log.info(
                "scheduled collect %s: %s (run=%s, points=%s) %s",
                m["model_id"], m["status"], m.get("run_issued_utc"), m.get("points"), m.get("error") or "",
            )
    except Exception:  # pragma: no cover - scheduler must never crash the app
        log.exception("scheduled collection failed")


def start_scheduler(app, settings):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=timezone.utc)
    trigger = CronTrigger.from_crontab(settings.schedule_cron, timezone=timezone.utc)
    scheduler.add_job(scheduled_collect, trigger, args=[app], id="windy-collect", replace_existing=True)
    scheduler.start()
    log.info("scheduler started with cron %r (UTC)", settings.schedule_cron)
    return scheduler


def run_blocking(settings) -> None:
    """CLI mode: block and run the collection schedule (cron/systemd alternative)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from .main import create_app

    app = create_app(settings)
    scheduler = BlockingScheduler(timezone=timezone.utc)
    scheduler.add_job(scheduled_collect, CronTrigger.from_crontab(settings.schedule_cron, timezone=timezone.utc), args=[app], id="windy-collect", replace_existing=True)
    log.info("blocking scheduler started with cron %r (UTC) — Ctrl+C to stop", settings.schedule_cron)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
