"""
app/api/v1/endpoints/admin.py
==============================
Single file for every admin endpoint. Replaces:
  - admin.py                (control panel, live analytics, audit, roles, comments)
  - admin_analytics_db.py   (REMOVED — live OLTP used instead; snapshot only for trends)
  - admin_trends.py         (trend charts + etl-status, merged here)
  - admin_access.py         (page registry + access matrix, merged here)

Route map:
  LIVE ANALYTICS (reads OLTP directly — always fresh)
    GET  /admin/analytics/overview
    GET  /admin/analytics/top-posts
    GET  /admin/analytics/top-users
    GET  /admin/analytics/active-commenters

  SNAPSHOT TRENDS (reads analytics_db — updated on Refresh click)
    GET  /admin/analytics/trends/daily
    GET  /admin/analytics/trends/categories
    GET  /admin/analytics/etl-status
    POST /admin/run-snapshot          ← Refresh button calls this first, then re-fetches

  USERS
    GET    /admin/users
    PUT    /admin/users/{id}/role
    PUT    /admin/users/{id}/status
    DELETE /admin/users/{id}
    GET    /admin/users/unverified
    PUT    /admin/users/{id}/verify
    DELETE /admin/users/unverified/bulk

  POSTS
    GET    /admin/posts
    DELETE /admin/posts/{id}

  REPORTS
    GET  /admin/reports
    PUT  /admin/reports/{id}/status

  CATEGORIES
    GET    /admin/categories
    PUT    /admin/categories/{id}
    DELETE /admin/categories/{id}

  KEYWORDS
    GET    /admin/keywords
    DELETE /admin/keywords/{id}

  COMMENTS
    GET    /admin/comments
    DELETE /admin/comments/{id}

  ROLES
    GET    /admin/roles
    POST   /admin/roles
    PUT    /admin/roles/{id}
    DELETE /admin/roles/{id}

  AUDIT
    GET  /admin/audit/actions
    GET  /admin/audit/role-changes
    GET  /admin/audit/report-resolutions

  ACCESS CONTROL (page registry + permission matrix)
    GET  /admin/access/pages
    GET  /admin/access/matrix
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.db.database import get_users_db, get_posts_db, get_admin_db, get_analytics_db
from app.db.models import (
    # OLTP
    PlatformUserRecord, RoleRecord,
    PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, CategoryRecord, KeywordRecord,
    FollowCurrentRecord, PostReportRecord,
    # Analytics warehouse
    FactDailySnapshot, FactCategoryDaily, DimCategory,
    EtlRunLog, FactWeeklyDelta, FactTopPost, FactTopUser, FactTopCommenter,
    # Admin audit
    AdminActionLog, RoleChangeHistory, ReportResolution,
    # Audit helpers (moved from app/analytics/audit.py)
    log_admin_action, log_role_change, log_report_resolution,
)
from app.services.auth_service import RoleChecker
from app.core.page_registry import PAGE_REGISTRY, PERMISSION_CATALOG, role_can_access

log = logging.getLogger("gisviz.admin")

router = APIRouter()

ADMIN        = RoleChecker(["admin"])
ADMIN_EDITOR = RoleChecker(["admin", "editor"])


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


# ════════════════════════════════════════════════════════════════════
#  LIVE ANALYTICS  — reads OLTP directly, always fresh
# ════════════════════════════════════════════════════════════════════

@router.get("/analytics/overview")
def analytics_overview(
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    now       = datetime.now(timezone.utc)
    week_ago  = now - timedelta(days=7)
    two_weeks = now - timedelta(days=14)
    return {
        "totals": {
            "users":        users_db.query(PlatformUserRecord).count(),
            "posts":        posts_db.query(PostRecord).count(),
            "likes":        posts_db.query(PostLikeRecord).count(),
            "bookmarks":    posts_db.query(PostBookmarkRecord).count(),
            "comments":     posts_db.query(PostCommentRecord).count(),
            "follows":      users_db.query(FollowCurrentRecord).count(),
            "categories":   posts_db.query(CategoryRecord).count(),
            "keywords":     posts_db.query(KeywordRecord).count(),
            "open_reports": posts_db.query(PostReportRecord).filter(PostReportRecord.status == "open").count(),
        },
        "this_week": {
            "new_users": users_db.query(PlatformUserRecord).filter(PlatformUserRecord.created_timestamp >= week_ago).count(),
            "new_posts": posts_db.query(PostRecord).filter(PostRecord.created_timestamp >= week_ago).count(),
        },
        "last_week": {
            "new_users": users_db.query(PlatformUserRecord).filter(
                PlatformUserRecord.created_timestamp >= two_weeks,
                PlatformUserRecord.created_timestamp < week_ago,
            ).count(),
            "new_posts": posts_db.query(PostRecord).filter(
                PostRecord.created_timestamp >= two_weeks,
                PostRecord.created_timestamp < week_ago,
            ).count(),
        },
    }


@router.get("/analytics/top-posts")
def analytics_top_posts(
    by: str = Query("likes", regex="^(likes|bookmarks|comments)$"),
    limit: int = Query(10, ge=1, le=50),
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    if by == "likes":
        posts = posts_db.query(PostRecord).order_by(PostRecord.total_likes_count.desc()).limit(limit).all()
    elif by == "comments":
        posts = posts_db.query(PostRecord).order_by(PostRecord.total_comments_count.desc()).limit(limit).all()
    else:  # bookmarks
        bm = (posts_db.query(PostBookmarkRecord.post_id, func.count().label("cnt"))
              .group_by(PostBookmarkRecord.post_id)
              .order_by(func.count().desc()).limit(limit).all())
        ids = [r.post_id for r in bm]
        cnt = {r.post_id: r.cnt for r in bm}
        posts = posts_db.query(PostRecord).filter(PostRecord.post_id.in_(ids)).all()
        posts.sort(key=lambda p: cnt.get(p.post_id, 0), reverse=True)

    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(
                      PlatformUserRecord.user_id.in_({p.publisher_user_id for p in posts})).all()}
    return [
        {
            "post_id": str(p.post_id),
            "title": p.title,
            "publisher_handle": handle_map.get(p.publisher_user_id, "deleted_user"),
            "total_likes_count": p.total_likes_count,
            "total_comments_count": p.total_comments_count,
        }
        for p in posts
    ]


@router.get("/analytics/top-users")
def analytics_top_users(
    by: str = Query("followers", regex="^(followers|posts)$"),
    limit: int = Query(10, ge=1, le=50),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    if by == "followers":
        users = users_db.query(PlatformUserRecord).order_by(PlatformUserRecord.follower_count.desc()).limit(limit).all()
    else:
        users = users_db.query(PlatformUserRecord).order_by(PlatformUserRecord.post_count.desc()).limit(limit).all()
    return [
        {
            "user_id": str(u.user_id),
            "user_handle": u.user_handle,
            "follower_count": u.follower_count,
            "post_count": u.post_count,
            "role_name": u.role.name if u.role else "viewer",
        }
        for u in users
    ]


@router.get("/analytics/active-commenters")
def analytics_active_commenters(
    limit: int = Query(10, ge=1, le=50),
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    rows = (posts_db.query(PostCommentRecord.user_id, func.count().label("cnt"))
            .group_by(PostCommentRecord.user_id)
            .order_by(desc("cnt")).limit(limit).all())
    uid_list   = [r.user_id for r in rows]
    cnt_map    = {r.user_id: r.cnt for r in rows}
    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(
                      PlatformUserRecord.user_id.in_(uid_list)).all()}
    return [
        {
            "user_id": str(uid),
            "user_handle": handle_map.get(uid, "deleted_user"),
            "comment_count": cnt_map[uid],
        }
        for uid in uid_list
    ]


# ════════════════════════════════════════════════════════════════════
#  SNAPSHOT TRENDS  — reads analytics_db (updated via run-snapshot)
# ════════════════════════════════════════════════════════════════════

@router.get("/analytics/trends/daily")
def analytics_trends_daily(
    days: int = Query(90, ge=1, le=730),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows   = (a_db.query(FactDailySnapshot)
              .filter(FactDailySnapshot.snapshot_date >= cutoff)
              .order_by(FactDailySnapshot.snapshot_date.asc()).all())
    latest = a_db.query(func.max(FactDailySnapshot.snapshot_date)).scalar()
    stale  = None
    if latest is None:
        stale = "No snapshots yet — click Refresh to run the first snapshot."
    elif (datetime.now(timezone.utc).date() - latest).days >= 2:
        stale = f"Latest snapshot is {(datetime.now(timezone.utc).date() - latest).days} days old."
    return {
        "points": [
            {
                "date": r.snapshot_date.isoformat(),
                "total_users": r.total_users, "total_posts": r.total_posts,
                "total_likes": r.total_likes, "total_bookmarks": r.total_bookmarks,
                "total_comments": r.total_comments, "total_follows": r.total_follows,
                "open_reports": r.open_reports,
                "new_users": r.new_users, "new_posts": r.new_posts,
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
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows   = (a_db.query(FactCategoryDaily, DimCategory.label)
              .join(DimCategory, DimCategory.category_id == FactCategoryDaily.category_id)
              .filter(FactCategoryDaily.snapshot_date >= cutoff)
              .order_by(FactCategoryDaily.snapshot_date.asc()).all())
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
    runs = (a_db.query(EtlRunLog)
            .order_by(desc(EtlRunLog.started_timestamp)).limit(limit).all())
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
    Called by the Refresh button on the analytics page.
    Runs the ETL snapshot for today (UTC), which populates analytics_db
    so that trends/daily, trends/categories, and etl-status reflect
    the current state of users_db + posts_db.
    """
    from app.analytics.snapshot import run_daily_snapshot
    return run_daily_snapshot()


# ════════════════════════════════════════════════════════════════════
#  USERS
# ════════════════════════════════════════════════════════════════════

@router.get("/users")
def list_all_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    query = users_db.query(PlatformUserRecord)
    if q:
        query = query.filter(
            PlatformUserRecord.user_handle.ilike(f"%{q}%") |
            PlatformUserRecord.email_address.ilike(f"%{q}%")
        )
    total = query.count()
    users = query.order_by(PlatformUserRecord.created_timestamp.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "users": [
            {
                "user_id": str(u.user_id),
                "user_handle": u.user_handle,
                "email_address": u.email_address,
                "role_name": u.role.name if u.role else "viewer",
                "is_active": bool(u.is_active),
                "is_verified": bool(u.is_verified),
                "follower_count": u.follower_count,
                "post_count": u.post_count,
                "created_timestamp": u.created_timestamp,
            }
            for u in users
        ],
    }


class RoleUpdatePayload(BaseModel):
    role_name: str


@router.put("/users/{user_id}/role")
def update_user_role(
    user_id: uuid.UUID,
    payload: RoleUpdatePayload,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    role = users_db.query(RoleRecord).filter(RoleRecord.name == payload.role_name).first()
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{payload.role_name}' not found")
    old_role = user.role.name if user.role else None
    user.role_id = role.role_id
    users_db.commit()
    log_role_change(admin_db, subject_user_id=user.user_id,
                    changed_by_user_id=admin.user_id,
                    old_role=old_role, new_role=payload.role_name)
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="user.role_change", target_type="user", target_id=str(user_id),
                     payload={"old_role": old_role, "new_role": payload.role_name},
                     ip_address=_ip(request))
    return {"message": "Role updated", "user_handle": user.user_handle, "new_role": payload.role_name}


@router.put("/users/{user_id}/status")
def set_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = int(is_active)
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="user.status_change", target_type="user", target_id=str(user_id),
                     payload={"user_handle": user.user_handle, "is_active": int(is_active)},
                     ip_address=_ip(request))
    return {"message": f"User {'activated' if is_active else 'deactivated'}", "is_active": int(is_active)}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: uuid.UUID,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account here.")
    snapshot = {"user_handle": user.user_handle, "email": user.email_address,
                "role": user.role.name if user.role else None}
    users_db.delete(user)
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="user.delete", target_type="user", target_id=str(user_id),
                     payload=snapshot, ip_address=_ip(request))
    return {"message": f"User {snapshot['user_handle']} permanently deleted"}


@router.get("/users/unverified")
def list_unverified_users(
    older_than_days: int = Query(7, ge=1),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    users  = (users_db.query(PlatformUserRecord)
              .filter(PlatformUserRecord.is_verified == 0,
                      PlatformUserRecord.created_timestamp <= cutoff)
              .order_by(PlatformUserRecord.created_timestamp.asc()).all())
    return [{"user_id": str(u.user_id), "user_handle": u.user_handle,
             "email_address": u.email_address, "created_timestamp": u.created_timestamp}
            for u in users]


@router.put("/users/{user_id}/verify")
def manually_verify_user(
    user_id: uuid.UUID,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_verified = 1
    user.verification_otp = None
    user.otp_expires_at   = None
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="user.verify", target_type="user", target_id=str(user_id),
                     payload={"user_handle": user.user_handle}, ip_address=_ip(request))
    return {"status": "verified", "user_handle": user.user_handle}


@router.delete("/users/unverified/bulk")
def bulk_delete_unverified(
    request: Request,
    older_than_days: int = Query(30, ge=7),
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    count  = (users_db.query(PlatformUserRecord)
              .filter(PlatformUserRecord.is_verified == 0,
                      PlatformUserRecord.created_timestamp <= cutoff)
              .delete(synchronize_session=False))
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="user.bulk_delete_unverified", target_type="user",
                     payload={"older_than_days": older_than_days, "deleted_count": count},
                     ip_address=_ip(request))
    return {"status": "deleted", "count": count}


# ════════════════════════════════════════════════════════════════════
#  POSTS
# ════════════════════════════════════════════════════════════════════

@router.get("/posts")
def list_all_posts(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    query = posts_db.query(PostRecord)
    if q:
        query = query.filter(PostRecord.title.ilike(f"%{q}%"))
    total = query.count()
    posts = query.order_by(PostRecord.created_timestamp.desc()).offset(skip).limit(limit).all()
    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(
                      PlatformUserRecord.user_id.in_({p.publisher_user_id for p in posts})).all()}
    return {
        "total": total,
        "posts": [
            {
                "post_id": str(p.post_id),
                "title": p.title,
                "publisher_handle": handle_map.get(p.publisher_user_id, "deleted_user"),
                "total_likes_count": p.total_likes_count,
                "total_comments_count": p.total_comments_count,
                "created_timestamp": p.created_timestamp,
            }
            for p in posts
        ],
    }


@router.delete("/posts/{post_id}")
def admin_delete_post(
    post_id: uuid.UUID,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    snapshot = {"title": post.title, "publisher_user_id": str(post.publisher_user_id)}
    posts_db.delete(post)
    posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="post.delete", target_type="post", target_id=str(post_id),
                     payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "post_id": str(post_id)}


# ════════════════════════════════════════════════════════════════════
#  REPORTS
# ════════════════════════════════════════════════════════════════════

@router.get("/reports")
def list_reports(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    query = posts_db.query(PostReportRecord)
    if status:
        query = query.filter(PostReportRecord.status == status)
    total   = query.count()
    reports = query.order_by(PostReportRecord.created_timestamp.desc()).offset(skip).limit(limit).all()
    uid_set = {r.reporter_user_id for r in reports}
    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(
                      PlatformUserRecord.user_id.in_(uid_set)).all()}
    return {
        "total": total,
        "reports": [
            {
                "report_id": str(r.report_id),
                "post_id": str(r.post_id),
                "reporter_handle": handle_map.get(r.reporter_user_id, "deleted_user"),
                "reason": r.reason,
                "status": r.status,
                "created_timestamp": r.created_timestamp,
            }
            for r in reports
        ],
    }


class ReportStatusPayload(BaseModel):
    status: str   # open | resolved | dismissed
    notes: Optional[str] = None


@router.put("/reports/{report_id}/status")
def update_report_status(
    report_id: uuid.UUID,
    payload: ReportStatusPayload,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    if payload.status not in ("open", "resolved", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be: open, resolved, dismissed")
    report = posts_db.query(PostReportRecord).filter(PostReportRecord.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.status = payload.status
    posts_db.commit()
    if payload.status in ("resolved", "dismissed"):
        log_report_resolution(admin_db, report_id=report_id,
                              resolved_by_user_id=admin.user_id,
                              resolution=payload.status,
                              post_id=report.post_id,
                              notes=payload.notes)
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="report.status_change", target_type="report",
                     target_id=str(report_id),
                     payload={"status": payload.status, "post_id": str(report.post_id)},
                     ip_address=_ip(request))
    return {"status": payload.status, "report_id": str(report_id)}


# ════════════════════════════════════════════════════════════════════
#  CATEGORIES
# ════════════════════════════════════════════════════════════════════

@router.get("/categories")
def list_all_categories(
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    cats = posts_db.query(CategoryRecord).order_by(CategoryRecord.usage_count.desc()).all()
    return [{"category_id": c.category_id, "slug": c.slug,
             "label": c.label, "usage_count": c.usage_count} for c in cats]


class CategoryEditPayload(BaseModel):
    label: str
    slug: str


@router.put("/categories/{category_id}")
def update_category(
    category_id: int,
    payload: CategoryEditPayload,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    cat = posts_db.query(CategoryRecord).filter(CategoryRecord.category_id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    old = {"label": cat.label, "slug": cat.slug}
    cat.label = payload.label
    cat.slug  = payload.slug
    posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="category.update", target_type="category",
                     target_id=str(category_id),
                     payload={"before": old, "after": {"label": cat.label, "slug": cat.slug}},
                     ip_address=_ip(request))
    return {"category_id": category_id, "label": cat.label, "slug": cat.slug}


@router.delete("/categories/{category_id}")
def delete_category(
    category_id: int,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    cat = posts_db.query(CategoryRecord).filter(CategoryRecord.category_id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    snapshot = {"label": cat.label, "slug": cat.slug}
    posts_db.delete(cat)
    posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="category.delete", target_type="category",
                     target_id=str(category_id), payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "category_id": category_id}


# ════════════════════════════════════════════════════════════════════
#  KEYWORDS
# ════════════════════════════════════════════════════════════════════

@router.get("/keywords")
def list_all_keywords(
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    kws = posts_db.query(KeywordRecord).order_by(KeywordRecord.usage_count.desc()).all()
    return [{"keyword_id": k.keyword_id, "word": k.word, "usage_count": k.usage_count} for k in kws]


@router.delete("/keywords/{keyword_id}")
def delete_keyword(
    keyword_id: int,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    kw = posts_db.query(KeywordRecord).filter(KeywordRecord.keyword_id == keyword_id).first()
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    snapshot = {"word": kw.word}
    posts_db.delete(kw)
    posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="keyword.delete", target_type="keyword",
                     target_id=str(keyword_id), payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "keyword_id": keyword_id}


# ════════════════════════════════════════════════════════════════════
#  COMMENTS
# ════════════════════════════════════════════════════════════════════

@router.get("/comments")
def list_all_comments(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    query = posts_db.query(PostCommentRecord)
    if q:
        query = query.filter(PostCommentRecord.content.ilike(f"%{q}%"))
    total    = query.count()
    comments = query.order_by(desc(PostCommentRecord.created_timestamp)).offset(skip).limit(limit).all()
    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(
                      PlatformUserRecord.user_id.in_({c.user_id for c in comments})).all()}
    return {
        "total": total,
        "comments": [
            {
                "comment_id": str(c.comment_id),
                "post_id": str(c.post_id),
                "user_id": str(c.user_id),
                "user_handle": handle_map.get(c.user_id, "deleted_user"),
                "content": c.content,
                "is_edited": bool(c.is_edited),
                "created_timestamp": c.created_timestamp,
            }
            for c in comments
        ],
    }


@router.delete("/comments/{comment_id}")
def delete_comment(
    comment_id: uuid.UUID,
    request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    comment = posts_db.query(PostCommentRecord).filter(PostCommentRecord.comment_id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    snapshot = {"content": comment.content, "post_id": str(comment.post_id),
                "author_user_id": str(comment.user_id)}
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == comment.post_id).first()
    if post and post.total_comments_count > 0:
        post.total_comments_count -= 1
    posts_db.delete(comment)
    posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="comment.delete", target_type="comment",
                     target_id=str(comment_id), payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "comment_id": str(comment_id)}


# ════════════════════════════════════════════════════════════════════
#  ROLES
# ════════════════════════════════════════════════════════════════════

class RolePayload(BaseModel):
    name: str
    permissions: dict


@router.get("/roles")
def list_roles(
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    roles = users_db.query(RoleRecord).order_by(RoleRecord.role_id).all()
    return [{"role_id": r.role_id, "name": r.name,
             "permissions": r.permissions, "user_count": len(r.users)} for r in roles]


@router.post("/roles")
def create_role(
    payload: RolePayload,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    if users_db.query(RoleRecord).filter(RoleRecord.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Role already exists")
    role = RoleRecord(name=payload.name.strip().lower(), permissions=payload.permissions)
    users_db.add(role)
    users_db.commit()
    users_db.refresh(role)
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.create", target_type="role", target_id=str(role.role_id),
                     payload={"name": role.name}, ip_address=_ip(request))
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.put("/roles/{role_id}")
def update_role(
    role_id: int,
    payload: RolePayload,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    old = {"name": role.name, "permissions": role.permissions}
    role.name        = payload.name.strip().lower()
    role.permissions = payload.permissions
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.update", target_type="role", target_id=str(role_id),
                     payload={"before": old, "after": {"name": role.name, "permissions": role.permissions}},
                     ip_address=_ip(request))
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: int,
    request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.name in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Cannot delete 'admin' or 'viewer' role.")
    if role.users:
        raise HTTPException(status_code=400, detail=f"Role has {len(role.users)} users. Reassign first.")
    snapshot = {"name": role.name}
    users_db.delete(role)
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.delete", target_type="role", target_id=str(role_id),
                     payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "name": snapshot["name"]}


# ════════════════════════════════════════════════════════════════════
#  AUDIT TRAIL
# ════════════════════════════════════════════════════════════════════

@router.get("/audit/actions")
def audit_actions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    action_type: Optional[str] = Query(None),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    query = admin_db.query(AdminActionLog)
    if action_type:
        query = query.filter(AdminActionLog.action_type == action_type)
    total = query.count()
    rows  = query.order_by(desc(AdminActionLog.occurred_timestamp)).offset(skip).limit(limit).all()
    return {
        "total": total,
        "actions": [
            {
                "action_id": str(r.action_id),
                "admin_handle": r.admin_handle,
                "action_type": r.action_type,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "payload": r.payload,
                "ip_address": r.ip_address,
                "occurred": r.occurred_timestamp,
            }
            for r in rows
        ],
    }


@router.get("/audit/role-changes")
def audit_role_changes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    rows = (admin_db.query(RoleChangeHistory)
            .order_by(desc(RoleChangeHistory.occurred_timestamp))
            .offset(skip).limit(limit).all())
    return [
        {
            "change_id": str(r.change_id),
            "subject_user_id": str(r.subject_user_id),
            "changed_by_user_id": str(r.changed_by_user_id),
            "old_role": r.old_role,
            "new_role": r.new_role,
            "occurred": r.occurred_timestamp,
        }
        for r in rows
    ]


@router.get("/audit/report-resolutions")
def audit_report_resolutions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    rows = (admin_db.query(ReportResolution)
            .order_by(desc(ReportResolution.occurred_timestamp))
            .offset(skip).limit(limit).all())
    return [
        {
            "resolution_id": str(r.resolution_id),
            "report_id": str(r.report_id),
            "post_id": str(r.post_id) if r.post_id else None,
            "resolved_by_user_id": str(r.resolved_by_user_id),
            "resolution": r.resolution,
            "notes": r.notes,
            "occurred": r.occurred_timestamp,
        }
        for r in rows
    ]


# ════════════════════════════════════════════════════════════════════
#  ACCESS CONTROL  — page registry + permission matrix
# ════════════════════════════════════════════════════════════════════

@router.get("/access/pages")
def list_pages(_: PlatformUserRecord = Depends(ADMIN)):
    """The page registry + the permission catalog the UI toggles."""
    return {
        "pages": [p.model_dump() for p in PAGE_REGISTRY],
        "permissions": PERMISSION_CATALOG,
    }


@router.get("/access/matrix")
def access_matrix(
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Read-only derived page × role access grid."""
    roles = users_db.query(RoleRecord).order_by(RoleRecord.role_id).all()
    role_cols = [{"role_id": r.role_id, "name": r.name, "user_count": len(r.users)} for r in roles]
    rows = []
    for page in PAGE_REGISTRY:
        access = {
            r.role_id: role_can_access(r.permissions or {}, page.required_permission)
            for r in roles
        }
        rows.append({
            "key": page.key, "label": page.label, "path": page.path,
            "required_permission": page.required_permission,
            "description": page.description, "access": access,
        })
    return {"roles": role_cols, "pages": rows}