"""
app/analytics/scheduler.py

Optional in-process nightly scheduler using APScheduler. This is the
zero-extra-container way to run the snapshot. Wire it into FastAPI's
lifespan (see the snippet in main.py). If you later adopt Airflow or
Dagster, just don't start this scheduler — the ETL logic is unchanged.

Requires: apscheduler  (add to requirements.txt)
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.analytics.snapshot import run_daily_snapshot

logger = logging.getLogger("gisviz.analytics.scheduler")

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    # Run at 02:15 UTC every day.
    _scheduler.add_job(
        run_daily_snapshot,
        trigger=CronTrigger(hour=2, minute=15),
        id="daily_snapshot",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("APScheduler started: daily_snapshot @ 02:15 UTC")
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")