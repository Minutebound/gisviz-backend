"""
app/api/v1/endpoints/admin.py  — complete replacement

Every mutating endpoint now:
  1. Snapshots the about-to-be-destroyed data into `payload`
  2. Performs the mutation on the live OLTP databases
  3. Writes a synchronous audit row to admin_db

The admin_db write is synchronous and committed immediately after
the primary mutation. If it fails we log the error but do NOT
roll back the primary — the mutation happened; we just lost the log.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.db.database import get_users_db, get_posts_db, get_admin_db, get_analytics_db
from app.db.models import (
    PlatformUserRecord, RoleRecord,
    PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, CategoryRecord, KeywordRecord,
    FollowCurrentRecord, PostReportRecord,
)
from app.db.analytics_models import (
    FactDailySnapshot, FactPostEngagement, FactCategoryDaily,
    DimPost, DimCategory, EtlRunLog,
    AdminActionLog, RoleChangeHistory, ReportResolution,
)
from app.analytics.audit import log_admin_action, log_role_change, log_report_resolution
from app.services.auth_service import RoleChecker

log = logging.getLogger("gisviz.admin")

router = APIRouter()

ADMIN        = RoleChecker(["admin"])
ADMIN_EDITOR = RoleChecker(["admin", "editor"])


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


# ════════════════════════════════════════════════════════════════════
#  LIVE ANALYTICS  (reads OLTP databases directly — never stale)
# ════════════════════════════════════════════════════════════════════

@router.get("/analytics/overview")
def analytics_overview(
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """Live platform health. Always reads live tables."""
    now       = datetime.now(timezone.utc)
    week_ago  = now - timedelta(days=7)
    two_weeks = now - timedelta(days=14)

    return {
        "totals": {
            "users":       users_db.query(PlatformUserRecord).count(),
            "posts":       posts_db.query(PostRecord).count(),
            "likes":       posts_db.query(PostLikeRecord).count(),
            "bookmarks":   posts_db.query(PostBookmarkRecord).count(),
            "comments":    posts_db.query(PostCommentRecord).count(),
            "follows":     users_db.query(FollowCurrentRecord).count(),
            "categories":  posts_db.query(CategoryRecord).count(),
            "keywords":    posts_db.query(KeywordRecord).count(),
            "open_reports": posts_db.query(PostReportRecord).filter(PostReportRecord.status == "open").count(),
        },
        "this_week": {
            "new_users": users_db.query(PlatformUserRecord).filter(PlatformUserRecord.created_timestamp >= week_ago).count(),
            "new_posts": posts_db.query(PostRecord).filter(PostRecord.created_timestamp >= week_ago).count(),
        },
        "last_week": {
            "new_users": users_db.query(PlatformUserRecord).filter(PlatformUserRecord.created_timestamp >= two_weeks, PlatformUserRecord.created_timestamp < week_ago).count(),
            "new_posts": posts_db.query(PostRecord).filter(PostRecord.created_timestamp >= two_weeks, PostRecord.created_timestamp < week_ago).count(),
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
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.total_likes_count)).limit(limit).all()
    elif by == "bookmarks":
        bm = (posts_db.query(PostBookmarkRecord.post_id, func.count().label("cnt"))
              .group_by(PostBookmarkRecord.post_id).order_by(desc("cnt")).limit(limit).all())
        cnt_map = {r.post_id: r.cnt for r in bm}
        posts = sorted(posts_db.query(PostRecord).filter(PostRecord.post_id.in_([r.post_id for r in bm])).all(),
                       key=lambda p: cnt_map.get(p.post_id, 0), reverse=True)
    else:
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.total_comments_count)).limit(limit).all()

    user_map = {u.user_id: u.user_handle for u in users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id.in_({p.publisher_user_id for p in posts})).all()}
    return [{"post_id": str(p.post_id), "title": p.title,
             "publisher_handle": user_map.get(p.publisher_user_id, "unknown"),
             "total_likes_count": p.total_likes_count,
             "total_comments_count": p.total_comments_count,
             "created_timestamp": p.created_timestamp} for p in posts]


@router.get("/analytics/top-users")
def analytics_top_users(
    by: str = Query("followers", regex="^(followers|posts)$"),
    limit: int = Query(10, ge=1, le=50),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    order = desc(PlatformUserRecord.follower_count) if by == "followers" else desc(PlatformUserRecord.post_count)
    users = users_db.query(PlatformUserRecord).order_by(order).limit(limit).all()
    return [{"user_id": str(u.user_id), "user_handle": u.user_handle,
             "follower_count": u.follower_count, "post_count": u.post_count,
             "role_name": u.role.name if u.role else "viewer"} for u in users]


@router.get("/analytics/active-commenters")
def analytics_active_commenters(
    limit: int = Query(10, ge=1, le=50),
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    rows = (posts_db.query(PostCommentRecord.user_id, func.count().label("cnt"))
            .group_by(PostCommentRecord.user_id).order_by(desc("cnt")).limit(limit).all())
    uid_list = [r.user_id for r in rows]
    cnt_map  = {r.user_id: r.cnt for r in rows}
    handle_map = {u.user_id: u.user_handle for u in
                  users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(uid_list)).all()}
    return [{"user_id": str(uid), "user_handle": handle_map.get(uid, "unknown"),
             "comment_count": cnt_map[uid]} for uid in uid_list]


# ════════════════════════════════════════════════════════════════════
#  HISTORICAL TRENDS  (reads analytics_db — up to 24h old)
# ════════════════════════════════════════════════════════════════════

@router.get("/analytics/trends/daily")
def analytics_trends_daily(
    days: int = Query(90, ge=1, le=730),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    from datetime import date as _date
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows = (a_db.query(FactDailySnapshot)
            .filter(FactDailySnapshot.snapshot_date >= cutoff)
            .order_by(FactDailySnapshot.snapshot_date.asc()).all())

    latest = a_db.query(func.max(FactDailySnapshot.snapshot_date)).scalar()
    stale = None
    if latest is None:
        stale = "No snapshots yet — run the ETL to populate trend charts."
    elif (datetime.now(timezone.utc).date() - latest).days >= 2:
        stale = f"Latest snapshot is {(datetime.now(timezone.utc).date() - latest).days} days old — ETL may be failing."

    return {
        "points": [{"date": r.snapshot_date.isoformat(), "total_users": r.total_users,
                    "total_posts": r.total_posts, "total_likes": r.total_likes,
                    "total_bookmarks": r.total_bookmarks, "total_comments": r.total_comments,
                    "total_follows": r.total_follows, "open_reports": r.open_reports,
                    "new_users": r.new_users, "new_posts": r.new_posts} for r in rows],
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
    rows = (a_db.query(FactCategoryDaily, DimCategory.label)
            .join(DimCategory, DimCategory.category_id == FactCategoryDaily.category_id)
            .filter(FactCategoryDaily.snapshot_date >= cutoff)
            .order_by(FactCategoryDaily.snapshot_date.asc()).all())
    return {"points": [{"date": fc.snapshot_date.isoformat(), "category_id": fc.category_id,
                        "label": label, "usage_count": fc.usage_count} for fc, label in rows]}


@router.get("/analytics/etl-status")
def analytics_etl_status(
    limit: int = Query(10, ge=1, le=50),
    a_db: Session = Depends(get_analytics_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    runs = (a_db.query(EtlRunLog).order_by(desc(EtlRunLog.started_timestamp)).limit(limit).all())
    return [{"run_id": str(r.run_id), "job_name": r.job_name,
             "snapshot_date": r.snapshot_date.isoformat() if r.snapshot_date else None,
             "status": r.status, "rows_written": r.rows_written,
             "error_message": r.error_message,
             "started": r.started_timestamp, "finished": r.finished_timestamp,
             "duration_seconds": float(r.duration_seconds) if r.duration_seconds else None} for r in runs]


# ════════════════════════════════════════════════════════════════════
#  AUDIT TRAIL  (reads admin_db — live, permanent)
# ════════════════════════════════════════════════════════════════════

@router.get("/audit/actions")
def audit_actions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    action_type: Optional[str] = Query(None),
    admin_user_id: Optional[uuid.UUID] = Query(None),
    target_id: Optional[str] = Query(None),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    q = admin_db.query(AdminActionLog)
    if action_type:   q = q.filter(AdminActionLog.action_type == action_type)
    if admin_user_id: q = q.filter(AdminActionLog.admin_user_id == admin_user_id)
    if target_id:     q = q.filter(AdminActionLog.target_id == str(target_id))
    total = q.count()
    rows  = q.order_by(desc(AdminActionLog.occurred_timestamp)).offset(skip).limit(limit).all()
    return {
        "total": total,
        "actions": [{"action_id": str(r.action_id), "admin_user_id": str(r.admin_user_id),
                     "admin_handle": r.admin_handle, "action_type": r.action_type,
                     "target_type": r.target_type, "target_id": r.target_id,
                     "payload": r.payload, "ip_address": r.ip_address,
                     "occurred": r.occurred_timestamp} for r in rows],
    }


@router.get("/audit/actions/summary")
def audit_actions_summary(
    days: int = Query(30, ge=1, le=365),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (admin_db.query(AdminActionLog.action_type, func.count().label("n"))
            .filter(AdminActionLog.occurred_timestamp >= cutoff)
            .group_by(AdminActionLog.action_type).order_by(desc("n")).all())
    return {"window_days": days, "by_action": [{"action_type": a, "count": n} for a, n in rows]}


@router.get("/audit/role-changes")
def audit_role_changes(
    skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    rows = (admin_db.query(RoleChangeHistory)
            .order_by(desc(RoleChangeHistory.occurred_timestamp)).offset(skip).limit(limit).all())
    return [{"change_id": str(r.change_id), "subject_user_id": str(r.subject_user_id),
             "changed_by_user_id": str(r.changed_by_user_id),
             "old_role": r.old_role, "new_role": r.new_role, "occurred": r.occurred_timestamp} for r in rows]


@router.get("/audit/report-resolutions")
def audit_report_resolutions(
    skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    admin_db: Session = Depends(get_admin_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    rows = (admin_db.query(ReportResolution)
            .order_by(desc(ReportResolution.occurred_timestamp)).offset(skip).limit(limit).all())
    return [{"resolution_id": str(r.resolution_id), "report_id": str(r.report_id),
             "post_id": str(r.post_id) if r.post_id else None,
             "resolved_by_user_id": str(r.resolved_by_user_id),
             "resolution": r.resolution, "notes": r.notes, "occurred": r.occurred_timestamp} for r in rows]


# ════════════════════════════════════════════════════════════════════
#  ROLES  (with audit logging)
# ════════════════════════════════════════════════════════════════════

class RolePayload(BaseModel):
    name: str
    permissions: dict


@router.get("/roles")
def list_roles(users_db: Session = Depends(get_users_db), _: PlatformUserRecord = Depends(ADMIN)):
    roles = users_db.query(RoleRecord).order_by(RoleRecord.role_id).all()
    return [{"role_id": r.role_id, "name": r.name, "permissions": r.permissions,
             "user_count": len(r.users)} for r in roles]


@router.post("/roles")
def create_role(
    payload: RolePayload, request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    if users_db.query(RoleRecord).filter(RoleRecord.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Role already exists")
    role = RoleRecord(name=payload.name.strip().lower(), permissions=payload.permissions)
    users_db.add(role); users_db.commit(); users_db.refresh(role)
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.create", target_type="role", target_id=str(role.role_id),
                     payload={"name": role.name}, ip_address=_ip(request))
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.put("/roles/{role_id}")
def update_role(
    role_id: int, payload: RolePayload, request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role: raise HTTPException(status_code=404, detail="Role not found")
    old = {"name": role.name, "permissions": role.permissions}
    role.name = payload.name.strip().lower(); role.permissions = payload.permissions
    users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.update", target_type="role", target_id=str(role_id),
                     payload={"before": old, "after": {"name": role.name, "permissions": role.permissions}},
                     ip_address=_ip(request))
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: int, request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role: raise HTTPException(status_code=404, detail="Role not found")
    if role.name in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Cannot delete 'admin' or 'viewer' role.")
    if role.users:
        raise HTTPException(status_code=400, detail=f"Role has {len(role.users)} user(s). Reassign first.")
    snapshot = {"name": role.name}
    users_db.delete(role); users_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="role.delete", target_type="role", target_id=str(role_id),
                     payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "name": snapshot["name"]}


# ════════════════════════════════════════════════════════════════════
#  COMMENTS  (with audit logging on delete)
# ════════════════════════════════════════════════════════════════════

@router.get("/comments")
def list_all_comments(
    skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    query = posts_db.query(PostCommentRecord)
    if q: query = query.filter(PostCommentRecord.content.ilike(f"%{q}%"))
    total    = query.count()
    comments = query.order_by(desc(PostCommentRecord.created_timestamp)).offset(skip).limit(limit).all()
    user_map = {u.user_id: u.user_handle for u in users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id.in_({c.user_id for c in comments})).all()}
    return {"total": total, "comments": [
        {"comment_id": str(c.comment_id), "post_id": str(c.post_id),
         "user_id": str(c.user_id), "user_handle": user_map.get(c.user_id, "unknown"),
         "content": c.content, "is_edited": bool(c.is_edited),
         "created_timestamp": c.created_timestamp} for c in comments]}


@router.delete("/comments/{comment_id}")
def delete_comment(
    comment_id: uuid.UUID, request: Request,
    posts_db: Session = Depends(get_posts_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    comment = posts_db.query(PostCommentRecord).filter(PostCommentRecord.comment_id == comment_id).first()
    if not comment: raise HTTPException(status_code=404, detail="Comment not found")
    snapshot = {"content": comment.content, "post_id": str(comment.post_id),
                "author_user_id": str(comment.user_id)}
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == comment.post_id).first()
    if post and post.total_comments_count > 0:
        post.total_comments_count -= 1
    posts_db.delete(comment); posts_db.commit()
    log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                     action_type="comment.delete", target_type="comment",
                     target_id=str(comment_id), payload=snapshot, ip_address=_ip(request))
    return {"status": "deleted", "comment_id": str(comment_id)}


# ════════════════════════════════════════════════════════════════════
#  UNVERIFIED USERS  (with audit logging)
# ════════════════════════════════════════════════════════════════════

@router.get("/users/unverified")
def list_unverified_users(
    older_than_days: int = Query(7, ge=1),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    users = (users_db.query(PlatformUserRecord)
             .filter(PlatformUserRecord.is_verified == 0,
                     PlatformUserRecord.created_timestamp <= cutoff)
             .order_by(PlatformUserRecord.created_timestamp.asc()).all())
    return [{"user_id": str(u.user_id), "user_handle": u.user_handle,
             "email_address": u.email_address, "created_timestamp": u.created_timestamp} for u in users]


@router.put("/users/{user_id}/verify")
def manually_verify_user(
    user_id: uuid.UUID, request: Request,
    users_db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    admin: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    user.is_verified = 1; user.verification_otp = None; user.otp_expires_at = None
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
    count = (users_db.query(PlatformUserRecord)
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
#  SNAPSHOT TRIGGER  (manual ETL run from the admin API)
# ════════════════════════════════════════════════════════════════════

@router.post("/run-snapshot")
def trigger_snapshot(
    request: Request,
    admin: PlatformUserRecord = Depends(ADMIN),
):
    """Manual ETL trigger — useful for backfills and dev."""
    from app.analytics.snapshot import run_daily_snapshot
    result = run_daily_snapshot()
    return result