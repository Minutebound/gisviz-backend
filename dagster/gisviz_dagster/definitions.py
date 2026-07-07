"""
gisviz_dagster/definitions.py

Dagster pipeline for the gisviz analytics ETL.
Verified to load cleanly against dagster==1.9.4.

THE ONE RULE that avoids every context-annotation error:
    Do NOT annotate the `context` parameter anywhere — not on @asset,
    not on @op, not on @sensor. Leave it bare. Dagster injects the
    correct context object at runtime regardless.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from dagster import (
    asset,
    define_asset_job,
    ScheduleDefinition,
    sensor,
    RunRequest,
    Definitions,
    op,
    job,
    Field,
    String,
)


# ════════════════════════════════════════════════════════════════════
#  ASSET — daily_snapshot
# ════════════════════════════════════════════════════════════════════

@asset(
    description="Platform-wide + per-post + per-category daily snapshot into analytics_db",
    group_name="analytics",
    config_schema={
        "snapshot_date": Field(
            String,
            default_value="",
            description="Date to snapshot as YYYY-MM-DD. Leave blank for today (UTC).",
            is_required=False,
        )
    },
)
def daily_snapshot(context) -> dict:
    from app.analytics.snapshot import run_daily_snapshot

    raw_date = (context.op_config or {}).get("snapshot_date", "").strip()

    target = None
    if raw_date:
        target = datetime.strptime(raw_date, "%Y-%m-%d").date()

    context.log.info("Starting snapshot for %s", target or "today (UTC)")
    result = run_daily_snapshot(target)
    context.log.info("Snapshot complete: %s", result)

    context.add_output_metadata({
        "snapshot_date": result["snapshot_date"],
        "rows_written":  result["rows_written"],
        "total_users":   result["totals"]["users"],
        "total_posts":   result["totals"]["posts"],
    })
    return result


# ════════════════════════════════════════════════════════════════════
#  JOBS
# ════════════════════════════════════════════════════════════════════

snapshot_job = define_asset_job(
    name="snapshot_job",
    selection=[daily_snapshot],
    description="Nightly analytics snapshot",
)


@op(
    config_schema={
        "start_date": Field(String, description="Start date YYYY-MM-DD", is_required=True),
        "end_date":   Field(String, description="End date YYYY-MM-DD (inclusive)", is_required=True),
    }
)
def backfill_op(context):
    from app.analytics.snapshot import run_daily_snapshot

    start = datetime.strptime(context.op_config["start_date"], "%Y-%m-%d").date()
    end   = datetime.strptime(context.op_config["end_date"],   "%Y-%m-%d").date()

    if end < start:
        raise ValueError("end_date must be >= start_date")

    results = []
    d = start
    while d <= end:
        context.log.info("Backfilling %s", d)
        result = run_daily_snapshot(d)
        results.append(result)
        d += timedelta(days=1)

    context.log.info("Backfill complete: %d days", len(results))
    return results


@job(description="Backfill analytics snapshots for a date range")
def backfill_job():
    backfill_op()


# ════════════════════════════════════════════════════════════════════
#  SCHEDULES
# ════════════════════════════════════════════════════════════════════

nightly_snapshot = ScheduleDefinition(
    job=snapshot_job,
    cron_schedule="15 2 * * *",
    execution_timezone="UTC",
    description="Daily analytics snapshot at 02:15 UTC",
)


# ════════════════════════════════════════════════════════════════════
#  SENSORS
# ════════════════════════════════════════════════════════════════════

@sensor(job=snapshot_job, minimum_interval_seconds=3600)
def etl_health_sensor(context):
    from app.db.database import AnalyticsSessionLocal
    from app.db.analytics_models import EtlRunLog
    from sqlalchemy import func as sqla_func

    db = AnalyticsSessionLocal()
    try:
        latest = db.query(sqla_func.max(EtlRunLog.started_timestamp)).scalar()
    finally:
        db.close()

    now = datetime.now(timezone.utc)
    if latest is None or (now - latest.replace(tzinfo=timezone.utc)) > timedelta(hours=26):
        context.log.error(
            "ETL health check FAILED — latest run: %s. Triggering catch-up.", latest
        )
        yield RunRequest(run_key=f"catchup-{now.date().isoformat()}")
    else:
        context.log.info("ETL healthy — last run: %s", latest)


# ════════════════════════════════════════════════════════════════════
#  DEFINITIONS
# ════════════════════════════════════════════════════════════════════

defs = Definitions(
    assets=[daily_snapshot],
    jobs=[snapshot_job, backfill_job],
    schedules=[nightly_snapshot],
    sensors=[etl_health_sensor],
)