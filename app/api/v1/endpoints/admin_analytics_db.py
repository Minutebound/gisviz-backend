"""
app/api/v1/endpoints/admin_analytics_db.py

DB-backed analytics endpoints. Serves the ENTIRE analytics page from
analytics_db (the latest snapshot), not live OLTP queries. Numbers are
"as of the last snapshot" — the warehouse-backed dashboard model.

These reuse the SAME paths as the old live endpoints, so the frontend
needs no path changes IF you stop registering the old ones. To switch:

  In main.py, REPLACE the old admin analytics registration. The old
  overview/top-posts/top-users/active-commenters live in admin.py's
  router. Since both share the /admin prefix and same paths, register
  THIS router INSTEAD of (or after) the admin router so these win.

  Cleanest: comment out the 4 live analytics endpoints in admin.py, then:
      from app.api.v1.endpoints import admin_analytics_db
      app.include_router(admin_analytics_db.router,
                         prefix=f"{settings.API_V1_STR}/admin",
                         tags=["Admin Analytics (DB)"])

All endpoints read the most recent snapshot_date present in analytics_db.
If no snapshot exists yet, they return zeros / empty lists (not errors).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.db.database import get_analytics_db
from app.db.models import PlatformUserRecord
from app.db.analytics_models import (
    FactDailySnapshot, FactWeeklyDelta,
    FactTopPost, FactTopUser, FactTopCommenter,
)
from app.services.auth_service import RoleChecker

router = APIRouter()

ADMIN = RoleChecker(["admin"])


def _latest_date(a_db: Session):
    return a_db.query(func.max(FactDailySnapshot.snapshot_date)).scalar()


@router.get("/analytics/overview")
def analytics_overview(
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Overview totals + weekly deltas, as of the latest snapshot."""
    latest = _latest_date(a_db)
    if latest is None:
        empty = {"users": 0, "posts": 0, "likes": 0, "bookmarks": 0,
                 "comments": 0, "follows": 0, "categories": 0,
                 "keywords": 0, "open_reports": 0}
        return {"totals": empty,
                "this_week": {"new_users": 0, "new_posts": 0},
                "last_week": {"new_users": 0, "new_posts": 0},
                "as_of": None}

    snap = a_db.query(FactDailySnapshot).filter(
        FactDailySnapshot.snapshot_date == latest
    ).first()
    wd = a_db.query(FactWeeklyDelta).filter(
        FactWeeklyDelta.snapshot_date == latest
    ).first()

    return {
        "totals": {
            "users":       snap.total_users,
            "posts":       snap.total_posts,
            "likes":       snap.total_likes,
            "bookmarks":   snap.total_bookmarks,
            "comments":    snap.total_comments,
            "follows":     snap.total_follows,
            "categories":  snap.total_categories,
            "keywords":    snap.total_keywords,
            "open_reports": snap.open_reports,
        },
        "this_week": {
            "new_users": wd.this_week_new_users if wd else snap.new_users,
            "new_posts": wd.this_week_new_posts if wd else snap.new_posts,
        },
        "last_week": {
            "new_users": wd.last_week_new_users if wd else 0,
            "new_posts": wd.last_week_new_posts if wd else 0,
        },
        "as_of": latest.isoformat(),
    }


@router.get("/analytics/top-posts")
def analytics_top_posts(
    by: str = Query("likes", regex="^(likes|bookmarks|comments)$"),
    limit: int = Query(10, ge=1, le=50),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    latest = _latest_date(a_db)
    if latest is None:
        return []
    rows = (a_db.query(FactTopPost)
            .filter(FactTopPost.snapshot_date == latest, FactTopPost.rank_by == by)
            .order_by(FactTopPost.rank.asc()).limit(limit).all())
    return [
        {
            "post_id": str(r.post_id),
            "title": r.title,
            "publisher_handle": r.publisher_handle,
            "total_likes_count": r.total_likes_count,
            "total_comments_count": r.total_comments_count,
        }
        for r in rows
    ]


@router.get("/analytics/top-users")
def analytics_top_users(
    by: str = Query("followers", regex="^(followers|posts)$"),
    limit: int = Query(10, ge=1, le=50),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    latest = _latest_date(a_db)
    if latest is None:
        return []
    rows = (a_db.query(FactTopUser)
            .filter(FactTopUser.snapshot_date == latest, FactTopUser.rank_by == by)
            .order_by(FactTopUser.rank.asc()).limit(limit).all())
    return [
        {
            "user_id": str(r.user_id),
            "user_handle": r.user_handle,
            "follower_count": r.follower_count,
            "post_count": r.post_count,
            "role_name": r.role_name or "viewer",
        }
        for r in rows
    ]


@router.get("/analytics/active-commenters")
def analytics_active_commenters(
    limit: int = Query(10, ge=1, le=50),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    latest = _latest_date(a_db)
    if latest is None:
        return []
    rows = (a_db.query(FactTopCommenter)
            .filter(FactTopCommenter.snapshot_date == latest)
            .order_by(FactTopCommenter.rank.asc()).limit(limit).all())
    return [
        {
            "user_id": str(r.user_id),
            "user_handle": r.user_handle,
            "comment_count": r.comment_count,
        }
        for r in rows
    ]