"""
app/api/v1/endpoints/admin_trends.py

Additive router for the snapshot-fed trend charts. Kept SEPARATE from
admin.py so your existing working admin router is untouched.

These endpoints read analytics_db (populated by the Dagster ETL) — the
only analytics data allowed to be up to 24h stale, because historical
trends cannot come from a live COUNT(*).

Register in main.py, right after the existing admin router line:

    from app.api.v1.endpoints import admin_trends
    app.include_router(
        admin_trends.router,
        prefix=f"{settings.API_V1_STR}/admin",
        tags=["Admin Trends"],
    )

Both routers share the same /admin prefix, so the paths become:
    /api/v1/admin/analytics/trends/daily
    /api/v1/admin/analytics/trends/categories
    /api/v1/admin/analytics/etl-status
"""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.db.database import get_analytics_db
from app.db.models import PlatformUserRecord
from app.db.analytics_models import (
    FactDailySnapshot, FactCategoryDaily, DimCategory, EtlRunLog,
)
from app.services.auth_service import RoleChecker

router = APIRouter()

ADMIN = RoleChecker(["admin"])


@router.get("/analytics/trends/daily")
def analytics_trends_daily(
    days: int = Query(90, ge=1, le=730),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Time series of platform-wide daily snapshots for the growth charts."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows = (
        a_db.query(FactDailySnapshot)
        .filter(FactDailySnapshot.snapshot_date >= cutoff)
        .order_by(FactDailySnapshot.snapshot_date.asc())
        .all()
    )

    latest = a_db.query(func.max(FactDailySnapshot.snapshot_date)).scalar()
    stale = None
    if latest is None:
        stale = "No snapshots yet — run the ETL to populate trend charts."
    else:
        lag = (datetime.now(timezone.utc).date() - latest).days
        if lag >= 2:
            stale = f"Latest snapshot is {lag} days old — the ETL may be failing."

    return {
        "points": [
            {
                "date": r.snapshot_date.isoformat(),
                "total_users": r.total_users,
                "total_posts": r.total_posts,
                "total_likes": r.total_likes,
                "total_bookmarks": r.total_bookmarks,
                "total_comments": r.total_comments,
                "total_follows": r.total_follows,
                "open_reports": r.open_reports,
                "new_users": r.new_users,
                "new_posts": r.new_posts,
            }
            for r in rows
        ],
        "count": len(rows),
        "stale_warning": stale,
    }


@router.get("/analytics/trends/categories")
def analytics_category_trends(
    days: int = Query(30, ge=1, le=365),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Per-category usage over time (for a stacked/multi-line chart)."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows = (
        a_db.query(FactCategoryDaily, DimCategory.label)
        .join(DimCategory, DimCategory.category_id == FactCategoryDaily.category_id)
        .filter(FactCategoryDaily.snapshot_date >= cutoff)
        .order_by(FactCategoryDaily.snapshot_date.asc())
        .all()
    )
    return {
        "points": [
            {
                "date": fc.snapshot_date.isoformat(),
                "category_id": fc.category_id,
                "label": label,
                "usage_count": fc.usage_count,
                "post_count": fc.post_count,
            }
            for fc, label in rows
        ]
    }


@router.get("/analytics/etl-status")
def analytics_etl_status(
    limit: int = Query(10, ge=1, le=50),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Recent ETL run history — lets the admin see if snapshots are healthy."""
    runs = (
        a_db.query(EtlRunLog)
        .order_by(desc(EtlRunLog.started_timestamp))
        .limit(limit)
        .all()
    )
    return [
        {
            "run_id": str(r.run_id),
            "job_name": r.job_name,
            "snapshot_date": r.snapshot_date.isoformat() if r.snapshot_date else None,
            "status": r.status,
            "rows_written": r.rows_written,
            "error_message": r.error_message,
            "started": r.started_timestamp,
            "finished": r.finished_timestamp,
            "duration_seconds": float(r.duration_seconds) if r.duration_seconds else None,
        }
        for r in runs
    ]


@router.post("/run-snapshot")
def trigger_snapshot(
    _: PlatformUserRecord = Depends(ADMIN),
):
    """
    Manual ETL trigger — runs the same snapshot function Dagster calls.
    Handy for the first data point, backfills, or dev, without opening Dagit.
    """
    from app.analytics.snapshot import run_daily_snapshot
    return run_daily_snapshot()