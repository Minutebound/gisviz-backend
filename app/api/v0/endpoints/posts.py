import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi_cache import FastAPICache
from fastapi_cache.decorator import cache
from sqlalchemy.orm import Session
from sqlalchemy import desc
import re
import secrets

from app.db.database import get_posts_db, get_users_db
from app.db.models import (
    PlatformUserRecord, PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, PostReportRecord, RoleRecord,
)
from app.schemas.post_schema import (
    PostResponse, PostPayload, LikeResponse, BookmarkResponse,
    CommentPayload, CommentData, PostReportPayload, PostReportResponse,ReportStatusPayload
)
from app.services.post_service import post_service
from app.services.cache_service import cache_service
from app.services.auth_service import (
    get_current_authenticated_user,
    get_optional_current_user,
    RoleChecker,
)

router = APIRouter()

# TTL constants in seconds
STREAM_TTL = 30
POST_TTL   = 86400
SEARCH_TTL = 86400


# ── Cache helpers ─────────────────────────────────────────────────────────────

async def _invalidate_comments(post_id: str) -> None:
    try:
        await FastAPICache.get_backend().clear(key=f"gisviz-cache:posts:comments:{post_id}")
    except Exception as exc:
        print(f"[cache] comments invalidation failed for {post_id}: {exc}")


async def _invalidate_user_posts(handle: str) -> None:
    try:
        backend = FastAPICache.get_backend()
        for skip in ("0", "50", "100"):
            await backend.clear(key=f"gisviz-cache:posts:user:{handle.lower()}:{skip}:50")
    except Exception as exc:
        print(f"[cache] user posts invalidation failed for {handle}: {exc}")


# -------------------------------------------------------------------
#  STREAM
#  Per-user (is_liked / is_bookmarked) so we cannot use @cache here.
#  Client-side 30-second TTL in api.ts prevents hammering.
# -------------------------------------------------------------------
@router.get("/stream", response_model=List[PostResponse])
async def fetch_post_stream(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    current_user_id = current_user.user_id if current_user else None
    return post_service.retrieve_global_stream(
        posts_db=posts_db,
        users_db=users_db,
        skip=skip,
        limit=limit,
        current_user_id=current_user_id,
    )

# -------------------------------------------------------------------
#  TRENDING (IDs only — kept for ETL / internal use)
# -------------------------------------------------------------------
@router.get("/trending", response_model=List[str])
async def trending_posts(n: int = Query(10, ge=1, le=50)):
    trending = cache_service.get("trending_posts")
    return trending[:n] if trending else []


# -------------------------------------------------------------------
#  TRENDING FULL — returns complete PostResponse[]
#
#  Strategy:
#  1. Try Redis "trending_posts" key (list of post_id strings set by ETL)
#  2. If cache hit → fetch those specific posts from DB, preserve order
#  3. If cache miss (ETL hasn't run yet) → fall back to top-N by
#     total_likes_count directly from DB so the tab is never empty
#
#  Per-user is_liked / is_bookmarked included when JWT present.
#  Client-side 2-minute TTL in api.ts prevents hammering.
# -------------------------------------------------------------------
@router.get("/trending-full", response_model=List[PostResponse])
async def trending_posts_full(
    n: int = Query(20, ge=1, le=50),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    current_user_id = current_user.user_id if current_user else None
    trending_ids = cache_service.get("trending_posts")

    if trending_ids:
        id_list = [uuid.UUID(i) for i in trending_ids[:n]]
        posts = posts_db.query(PostRecord).filter(
            PostRecord.post_id.in_(id_list),
            PostRecord.is_active == 1,   
        ).all()
        # preserve Redis order
        order = {pid: idx for idx, pid in enumerate(id_list)}
        posts = sorted(posts, key=lambda p: order.get(p.post_id, 999))
    else:
        posts = (
            posts_db.query(PostRecord)
            .filter(PostRecord.is_active == 1)   
            .order_by(desc(PostRecord.total_likes_count))
            .limit(n)
            .all()
        )

    return [
        post_service._format_post_response(posts_db, users_db, p, current_user_id)
        for p in posts
    ]

# -------------------------------------------------------------------
#  SEARCH
# -------------------------------------------------------------------
@router.get("/search", response_model=List[PostResponse])
async def search_post_stream(
    q: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    current_user_id = current_user.user_id if current_user else None
    posts = (
        posts_db.query(PostRecord)
        .filter(
            (PostRecord.title.ilike(f"%{q}%")) | (PostRecord.description.ilike(f"%{q}%"))
        )
        .order_by(desc(PostRecord.created_timestamp))
        .offset(skip).limit(limit).all()
    )
    return [
        post_service._format_post_response(posts_db, users_db, p, current_user_id)
        for p in posts
    ]


# -------------------------------------------------------------------
#  USER POSTS
# -------------------------------------------------------------------
@router.get("/user/{handle}", response_model=List[PostResponse])
async def get_user_posts(
    handle: str,
    skip: int = 0,
    limit: int = 50,
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    user = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle == handle
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    query = posts_db.query(PostRecord).filter(
        PostRecord.publisher_user_id == user.user_id
    )

    # Profile owner sees all their posts (including inactive).
    # Everyone else only sees active posts.
    is_own_profile = (
        current_user is not None
        and current_user.user_id == user.user_id
    )
    if not is_own_profile:
        query = query.filter(PostRecord.is_active == 1)

    posts = query.order_by(PostRecord.created_timestamp.desc()).offset(skip).limit(limit).all()
    current_user_id = current_user.user_id if current_user else None
    return [
        post_service._format_post_response(posts_db, users_db, p, current_user_id)
        for p in posts
    ]


# -------------------------------------------------------------------
#  USER BOOKMARKS  (authenticated, own profile only)
# -------------------------------------------------------------------
@router.get("/user/{handle}/bookmarks", response_model=List[PostResponse])
async def get_user_bookmarks(
    handle: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    target = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle == handle
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="You can only view your own bookmarks")

    bookmarks = (
        posts_db.query(PostBookmarkRecord)
        .filter(PostBookmarkRecord.user_id == current_user.user_id)
        .order_by(desc(PostBookmarkRecord.created_timestamp))
        .offset(skip).limit(limit).all()
    )
    if not bookmarks:
        return []

    post_map = {
        p.post_id: p for p in
        posts_db.query(PostRecord).filter(
            PostRecord.post_id.in_([b.post_id for b in bookmarks])
        ).all()
    }
    results = []
    for bm in bookmarks:
        post = post_map.get(bm.post_id)
        if post:
            results.append(
                post_service._format_post_response(posts_db, users_db, post, current_user.user_id)
            )
    return results


# -------------------------------------------------------------------
#  SINGLE POST
#  Per-user flags mean we cannot share a single cache entry across
#  all users — @cache omitted. Client TTL handles deduplication.
# -------------------------------------------------------------------
@router.get("/{post_id}", response_model=PostResponse)
async def get_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.is_active != 1:
        raise HTTPException(status_code=404, detail="Post not found")  # treat inactive as non-existent publicly
    current_user_id = current_user.user_id if current_user else None
    return post_service._format_post_response(posts_db, users_db, post, current_user_id)

# -------------------------------------------------------------------
#  POST CRUD
# -------------------------------------------------------------------
@router.post("", response_model=PostResponse, status_code=201)
async def create_post(
    payload: PostPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    new_post = await post_service.create_post(
        posts_db=posts_db, users_db=users_db,
        user_id=current_user.user_id, payload=payload,
    )
    current_user.post_count = (current_user.post_count or 0) + 1
    if current_user.role and current_user.role.name == "viewer":
        pub_role = users_db.query(RoleRecord).filter(RoleRecord.name == "publisher").first()
        if pub_role:
            current_user.role_id = pub_role.role_id
            if not current_user.title:
                current_user.title = "Platform Publisher"
    users_db.commit()
    await _invalidate_user_posts(current_user.user_handle)
    return new_post


@router.put("/{post_id}", response_model=PostResponse)
async def update_post(
    post_id: uuid.UUID, payload: PostPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    result = await post_service.update_post(
        posts_db=posts_db, users_db=users_db,
        post_id=post_id, payload=payload, current_user=current_user,
    )
    await _invalidate_user_posts(current_user.user_handle)
    return result


@router.delete("/{post_id}")
async def delete_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),    # ← ADD this dependency
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return await post_service.delete_post(
        posts_db=posts_db,
        post_id=post_id,
        current_user=current_user,
        users_db=users_db,    # ← pass it
    )

@router.put("/{post_id}/status")
async def set_post_status(
    post_id: uuid.UUID,
    is_active: bool,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    post.is_active = 1 if is_active else 0
    posts_db.commit()
    return {"post_id": str(post_id), "is_active": post.is_active}


# -------------------------------------------------------------------
#  MODERATION
# -------------------------------------------------------------------
@router.post("/{post_id}/report", response_model=PostReportResponse, status_code=201)
async def report_post(
    post_id: uuid.UUID, payload: PostReportPayload,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    report = PostReportRecord(
        post_id=post_id, reporter_user_id=current_user.user_id, reason=payload.reason,
    )
    posts_db.add(report)
    posts_db.commit()
    posts_db.refresh(report)
    return report

@router.post("/{post_id}/missing-visual")
def report_missing_visual(post_id: str, db: Session = Depends(get_posts_db)):
    """
    Self-healing endpoint triggered by the frontend when an image 404s.
    Verifies the file is actually missing before deactivating the post.
    """
    post = db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    if not post.visual_image_path:
        return {"status": "ignored", "detail": "Post does not require a visual."}

    # Remove the leading slash to make it a valid local path (e.g., "uploads/visuals/...")
    local_path = post.visual_image_path.lstrip("/")
    
    # SECURITY CHECK: Verify the file is actually missing!
    if os.path.exists(local_path) and os.path.isfile(local_path):
        # A user's browser glitched, or someone is trying to maliciously delete posts.
        return {"status": "ignored", "detail": "Image exists on server, ignoring request."}
    
    # If the file is genuinely missing, auto-deactivate
    post.is_active = 0 # Use False if your DB column is a Boolean
    db.commit()
    
    return {"status": "deactivated", "detail": "Missing visual confirmed, post deactivated."}

@router.get("/reports/all", response_model=List[PostReportResponse])
async def get_reports(
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor", "support"])),
):
    return posts_db.query(PostReportRecord).all()

# -------------------------------------------------------------------
#  KEYWORDS  (admin/editor management)
# -------------------------------------------------------------------
@router.get("/keywords")
async def list_keywords(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    """Admin/editor: paginated keyword list ordered by usage count."""
    from app.db.models import KeywordRecord
    keywords = (
        posts_db.query(KeywordRecord)
        .order_by(KeywordRecord.usage_count.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    total = posts_db.query(KeywordRecord).count()
    return {
        "total": total,
        "keywords": [
            {
                "keyword_id":  k.keyword_id,
                "word":        k.word,
                "usage_count": k.usage_count,
            }
            for k in keywords
        ],
    }

@router.delete("/keywords/{keyword_id}")
async def delete_keyword(
    keyword_id: int,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    """Admin only: delete a keyword and all its post links."""
    from app.db.models import KeywordRecord, PostKeywordLink
    kw = posts_db.query(KeywordRecord).filter(KeywordRecord.keyword_id == keyword_id).first()
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    posts_db.query(PostKeywordLink).filter(
        PostKeywordLink.keyword_id == keyword_id
    ).delete(synchronize_session=False)
    posts_db.delete(kw)
    posts_db.commit()
    return {"status": "deleted", "word": kw.word}


# -------------------------------------------------------------------
#  REPORT STATUS UPDATE  (admin/editor/support)
# -------------------------------------------------------------------
@router.put("/reports/{report_id}/status")
async def update_report_status(
    report_id: uuid.UUID,
    payload: ReportStatusPayload,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(
        RoleChecker(["admin", "editor", "support"])
    ),
):
    if payload.status not in ("open", "resolved", "dismissed"):
        raise HTTPException(
            status_code=400,
            detail="Invalid status. Use: open, resolved, dismissed",
        )
    report = posts_db.query(PostReportRecord).filter(
        PostReportRecord.report_id == report_id
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.status = payload.status
    posts_db.commit()
    return {"status": payload.status, "report_id": str(report_id)}


# -------------------------------------------------------------------
#  LIKES
# -------------------------------------------------------------------
@router.post("/{post_id}/like", response_model=LikeResponse)
async def toggle_like(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = posts_db.query(PostLikeRecord).filter(
        PostLikeRecord.post_id == post_id,
        PostLikeRecord.user_id == current_user.user_id,
    ).first()

    if existing:
        posts_db.delete(existing)
        post.total_likes_count = max(0, post.total_likes_count - 1)
        liked = False
    else:
        posts_db.add(PostLikeRecord(post_id=post_id, user_id=current_user.user_id))
        post.total_likes_count += 1
        liked = True

    posts_db.commit()
    return {
        "post_id": post_id,
        "user_id": current_user.user_id,
        "liked": liked,
        "total_likes_count": post.total_likes_count,
    }


# -------------------------------------------------------------------
#  BOOKMARKS
# -------------------------------------------------------------------
@router.post("/{post_id}/bookmark", response_model=BookmarkResponse)
async def toggle_bookmark(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Users cannot bookmark their own posts
    if post.publisher_user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="You cannot bookmark your own post.")

    existing = posts_db.query(PostBookmarkRecord).filter(
        PostBookmarkRecord.post_id == post_id,
        PostBookmarkRecord.user_id == current_user.user_id,
    ).first()

    if existing:
        posts_db.delete(existing)
        bookmarked = False
    else:
        posts_db.add(PostBookmarkRecord(post_id=post_id, user_id=current_user.user_id))
        bookmarked = True

    posts_db.commit()
    return {"post_id": post_id, "user_id": current_user.user_id, "bookmarked": bookmarked}


# -------------------------------------------------------------------
#  COMMENTS
# -------------------------------------------------------------------
@router.post("/{post_id}/comments", response_model=CommentData, status_code=201)
async def add_comment(
    post_id: uuid.UUID, payload: CommentPayload,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    new_comment = PostCommentRecord(
        post_id=post_id, user_id=current_user.user_id,
        parent_comment_id=payload.parent_comment_id, content=payload.content,
    )
    posts_db.add(new_comment)
    post.total_comments_count += 1
    posts_db.commit()
    posts_db.refresh(new_comment)
    await _invalidate_comments(str(post_id))
    return CommentData(
        comment_id=new_comment.comment_id, post_id=new_comment.post_id,
        user_id=new_comment.user_id, publisher_handle=current_user.user_handle,
        publisher_avatar_path=current_user.avatar_path,
        parent_comment_id=new_comment.parent_comment_id, content=new_comment.content,
        is_edited=False, created_timestamp=new_comment.created_timestamp, replies=[],
    )


@router.get("/{post_id}/comments", response_model=List[CommentData])
async def get_comments(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    all_comments = (
        posts_db.query(PostCommentRecord)
        .filter(PostCommentRecord.post_id == post_id)
        .order_by(PostCommentRecord.created_timestamp.asc()).all()
    )
    commenter_map = {
        u.user_id: u for u in
        users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id.in_(set(c.user_id for c in all_comments))
        ).all()
    }

    def _build(c: PostCommentRecord) -> CommentData:
        author = commenter_map.get(c.user_id)
        return CommentData(
            comment_id=c.comment_id, post_id=c.post_id,
            user_id=c.user_id,
            publisher_handle=author.user_handle if author else "deleted_user",
            publisher_avatar_path=author.avatar_path if author else None,
            parent_comment_id=c.parent_comment_id, content=c.content,
            is_edited=c.is_edited, created_timestamp=c.created_timestamp, replies=[],
        )

    top_level = {c.comment_id: _build(c) for c in all_comments if not c.parent_comment_id}
    for c in all_comments:
        if c.parent_comment_id and c.parent_comment_id in top_level:
            top_level[c.parent_comment_id].replies.append(_build(c))
    return list(top_level.values())

## Slug-based post retrieval for public sharing (SEO method instead of UUID)

@router.get("/slug/{slug}", response_model=PostResponse)
async def get_post_by_slug(
    slug: str,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.share_slug == slug).first()
    if not post or post.is_active != 1:
        raise HTTPException(status_code=404, detail="Post not found")
    current_user_id = current_user.user_id if current_user else None
    return post_service._format_post_response(posts_db, users_db, post, current_user_id)