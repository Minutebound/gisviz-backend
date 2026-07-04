"""
app/api/v1/endpoints/admin.py

Admin-only endpoints that don't fit into the existing domain routers.
All routes require the 'admin' role.

Registered in main.py as:
    app.include_router(admin.router, prefix=f"{settings.API_V1_STR}/admin", tags=["Admin"])
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.db.database import get_users_db, get_posts_db
from app.db.models import (
    PlatformUserRecord, RoleRecord, UserLocationRecord,
    PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, CategoryRecord, KeywordRecord,
    FollowCurrentRecord, PostReportRecord,
)
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()

ADMIN = RoleChecker(["admin"])
ADMIN_EDITOR = RoleChecker(["admin", "editor"])


# ════════════════════════════════════════════════════════════════════
#  ANALYTICS
# ════════════════════════════════════════════════════════════════════

@router.get("/analytics/overview")
def analytics_overview(
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """High-level platform health numbers."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    total_users      = users_db.query(PlatformUserRecord).count()
    total_posts      = posts_db.query(PostRecord).count()
    total_likes      = posts_db.query(PostLikeRecord).count()
    total_bookmarks  = posts_db.query(PostBookmarkRecord).count()
    total_comments   = posts_db.query(PostCommentRecord).count()
    total_follows    = users_db.query(FollowCurrentRecord).count()
    total_categories = posts_db.query(CategoryRecord).count()
    total_keywords   = posts_db.query(KeywordRecord).count()
    open_reports     = posts_db.query(PostReportRecord).filter(PostReportRecord.status == "open").count()

    new_users_this_week = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.created_timestamp >= week_ago
    ).count()
    new_posts_this_week = posts_db.query(PostRecord).filter(
        PostRecord.created_timestamp >= week_ago
    ).count()

    # Previous week for trend arrows
    two_weeks_ago = now - timedelta(days=14)
    new_users_last_week = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.created_timestamp >= two_weeks_ago,
        PlatformUserRecord.created_timestamp < week_ago,
    ).count()
    new_posts_last_week = posts_db.query(PostRecord).filter(
        PostRecord.created_timestamp >= two_weeks_ago,
        PostRecord.created_timestamp < week_ago,
    ).count()

    return {
        "totals": {
            "users":      total_users,
            "posts":      total_posts,
            "likes":      total_likes,
            "bookmarks":  total_bookmarks,
            "comments":   total_comments,
            "follows":    total_follows,
            "categories": total_categories,
            "keywords":   total_keywords,
            "open_reports": open_reports,
        },
        "this_week": {
            "new_users": new_users_this_week,
            "new_posts": new_posts_this_week,
        },
        "last_week": {
            "new_users": new_users_last_week,
            "new_posts": new_posts_last_week,
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
    """Most liked / bookmarked / commented posts."""
    if by == "likes":
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.total_likes_count)).limit(limit).all()
    elif by == "bookmarks":
        # Count bookmark rows per post
        bm_counts = (
            posts_db.query(PostBookmarkRecord.post_id, func.count().label("cnt"))
            .group_by(PostBookmarkRecord.post_id)
            .order_by(desc("cnt"))
            .limit(limit)
            .all()
        )
        post_ids = [r.post_id for r in bm_counts]
        cnt_map  = {r.post_id: r.cnt for r in bm_counts}
        posts_raw = posts_db.query(PostRecord).filter(PostRecord.post_id.in_(post_ids)).all()
        posts = sorted(posts_raw, key=lambda p: cnt_map.get(p.post_id, 0), reverse=True)
    else:  # comments
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.total_comments_count)).limit(limit).all()

    user_ids = list({p.publisher_user_id for p in posts})
    user_map = {
        u.user_id: u.user_handle
        for u in users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(user_ids)).all()
    }

    return [
        {
            "post_id":          str(p.post_id),
            "title":            p.title,
            "publisher_handle": user_map.get(p.publisher_user_id, "unknown"),
            "total_likes_count":    p.total_likes_count,
            "total_comments_count": p.total_comments_count,
            "created_timestamp":    p.created_timestamp,
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
    """Most followed / most prolific publishers."""
    if by == "followers":
        users = users_db.query(PlatformUserRecord).order_by(desc(PlatformUserRecord.follower_count)).limit(limit).all()
    else:
        users = users_db.query(PlatformUserRecord).order_by(desc(PlatformUserRecord.post_count)).limit(limit).all()

    return [
        {
            "user_id":        str(u.user_id),
            "user_handle":    u.user_handle,
            "follower_count": u.follower_count,
            "post_count":     u.post_count,
            "role_name":      u.role.name if u.role else "viewer",
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
    """Users who have commented the most."""
    rows = (
        posts_db.query(PostCommentRecord.user_id, func.count().label("cnt"))
        .group_by(PostCommentRecord.user_id)
        .order_by(desc("cnt"))
        .limit(limit)
        .all()
    )
    user_ids = [r.user_id for r in rows]
    cnt_map  = {r.user_id: r.cnt for r in rows}
    users    = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(user_ids)).all()
    handle_map = {u.user_id: u.user_handle for u in users}

    return [
        {"user_id": str(uid), "user_handle": handle_map.get(uid, "unknown"), "comment_count": cnt_map[uid]}
        for uid in user_ids
    ]


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
    return [
        {
            "role_id":     r.role_id,
            "name":        r.name,
            "permissions": r.permissions,
            "user_count":  len(r.users),
        }
        for r in roles
    ]


@router.post("/roles")
def create_role(
    payload: RolePayload,
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    if users_db.query(RoleRecord).filter(RoleRecord.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Role already exists")
    role = RoleRecord(name=payload.name.strip().lower(), permissions=payload.permissions)
    users_db.add(role)
    users_db.commit()
    users_db.refresh(role)
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.put("/roles/{role_id}")
def update_role(
    role_id: int,
    payload: RolePayload,
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    role.name        = payload.name.strip().lower()
    role.permissions = payload.permissions
    users_db.commit()
    return {"role_id": role.role_id, "name": role.name, "permissions": role.permissions}


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: int,
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    role = users_db.query(RoleRecord).filter(RoleRecord.role_id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.name in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Cannot delete the 'admin' or 'viewer' role.")
    if role.users:
        raise HTTPException(status_code=400, detail=f"Role has {len(role.users)} user(s). Reassign them first.")
    users_db.delete(role)
    users_db.commit()
    return {"status": "deleted", "name": role.name}


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

    user_ids = list({c.user_id for c in comments})
    user_map = {
        u.user_id: u.user_handle
        for u in users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(user_ids)).all()
    }

    return {
        "total": total,
        "comments": [
            {
                "comment_id":   str(c.comment_id),
                "post_id":      str(c.post_id),
                "user_id":      str(c.user_id),
                "user_handle":  user_map.get(c.user_id, "unknown"),
                "content":      c.content,
                "is_edited":    bool(c.is_edited),
                "created_timestamp": c.created_timestamp,
            }
            for c in comments
        ],
    }


@router.delete("/comments/{comment_id}")
def delete_comment(
    comment_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    _: PlatformUserRecord = Depends(ADMIN_EDITOR),
):
    comment = posts_db.query(PostCommentRecord).filter(
        PostCommentRecord.comment_id == comment_id
    ).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    # Decrement counter on parent post
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == comment.post_id).first()
    if post and post.total_comments_count > 0:
        post.total_comments_count -= 1
    posts_db.delete(comment)
    posts_db.commit()
    return {"status": "deleted", "comment_id": str(comment_id)}


# ════════════════════════════════════════════════════════════════════
#  UNVERIFIED USERS
# ════════════════════════════════════════════════════════════════════

@router.get("/users/unverified")
def list_unverified_users(
    older_than_days: int = Query(7, ge=1),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    users = (
        users_db.query(PlatformUserRecord)
        .filter(
            PlatformUserRecord.is_verified == 0,
            PlatformUserRecord.created_timestamp <= cutoff,
        )
        .order_by(PlatformUserRecord.created_timestamp.asc())
        .all()
    )
    return [
        {
            "user_id":      str(u.user_id),
            "user_handle":  u.user_handle,
            "email_address": u.email_address,
            "created_timestamp": u.created_timestamp,
        }
        for u in users
    ]


@router.put("/users/{user_id}/verify")
def manually_verify_user(
    user_id: uuid.UUID,
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_verified = 1
    user.verification_otp = None
    user.otp_expires_at   = None
    users_db.commit()
    return {"status": "verified", "user_handle": user.user_handle}


@router.delete("/users/unverified/bulk")
def bulk_delete_unverified(
    older_than_days: int = Query(30, ge=7),
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    count = (
        users_db.query(PlatformUserRecord)
        .filter(
            PlatformUserRecord.is_verified == 0,
            PlatformUserRecord.created_timestamp <= cutoff,
        )
        .delete(synchronize_session=False)
    )
    users_db.commit()
    return {"status": "deleted", "count": count}