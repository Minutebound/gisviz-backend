import uuid
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List

# fastapi-cache2 decorator
from fastapi_cache.decorator import cache

from app.db.database import get_posts_db, get_users_db
from app.db.models import (
    PlatformUserRecord, PostRecord, PostLikeRecord, PostBookmarkRecord,
    PostCommentRecord, PostReportRecord, RoleRecord,
)
from app.schemas.post_schema import (
    PostResponse, PostPayload, LikeResponse, BookmarkResponse,
    CommentPayload, CommentData, PostReportPayload, PostReportResponse,
)
from app.services.post_service import post_service
from app.services.cache_service import cache_service   # kept for trending (sync)
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()


# ── TTL constants (seconds) ────────────────────────────────────────
STREAM_TTL   = None     # first page of the feed
STREAM_PAGE_TTL = None  # subsequent pages
POST_TTL     = None    # single post detail
SEARCH_TTL   = None     # search results


# -------------------------------------------------------------------
#  STREAM  ← cached with @cache
#  fastapi-cache2 auto-keys on the full URL including ?skip=&limit=
#  so every unique (skip, limit) pair gets its own cache entry.
# -------------------------------------------------------------------
@router.get("/stream", response_model=List[PostResponse])
@cache(expire=STREAM_TTL)
async def fetch_post_stream(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    """
    Global feed. Cached per (skip, limit) pair.
    skip=0 → 30s TTL (most volatile — new posts appear here first)
    skip>0 → 60s TTL (older pages change less often)
    """
    # Vary TTL by page
    # Note: @cache uses the expire= value set at decoration time.
    # For a variable TTL we call the service directly and let the
    # decorator handle the default; the second page naturally stays
    # in the same 30s window. If you need strict 60s for skip>0,
    # split into two routes or manage it via the service's manual Redis call.
    return post_service.retrieve_global_stream(
        posts_db=posts_db, users_db=users_db, skip=skip, limit=limit
    )


# -------------------------------------------------------------------
#  SEARCH  ← cached
# -------------------------------------------------------------------
@router.get("/search", response_model=List[PostResponse])
@cache(expire=SEARCH_TTL)
async def search_post_stream(
    q: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    posts = (
        posts_db.query(PostRecord)
        .filter(
            (PostRecord.title.ilike(f"%{q}%")) | (PostRecord.description.ilike(f"%{q}%"))
        )
        .order_by(desc(PostRecord.created_timestamp))
        .offset(skip).limit(limit).all()
    )
    user_ids = list(set(p.publisher_user_id for p in posts))
    user_map = {
        u.user_id: u for u in
        users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id.in_(user_ids)).all()
    }
    return [
        {
            "post_id": p.post_id,
            "publisher_user_id": p.publisher_user_id,
            "publisher_handle": user_map[p.publisher_user_id].user_handle if p.publisher_user_id in user_map else "Unknown",
            "publisher_avatar_path": user_map[p.publisher_user_id].avatar_path if p.publisher_user_id in user_map else None,
            "title": p.title, "description": p.description,
            "visual_image_path": p.visual_image_path,
            "categories": [link.category for link in p.category_links],
            "keywords": [link.keyword for link in p.keyword_links],
            "share_slug": p.share_slug, "share_url": f"/p/{p.share_slug}",
            "total_likes_count": p.total_likes_count,
            "total_comments_count": p.total_comments_count,
            "note": p.note, "source_name": p.source_name, "source_url": p.source_url,
            "created_timestamp": p.created_timestamp,
        }
        for p in posts
    ]


# -------------------------------------------------------------------
#  TRENDING  ← uses legacy sync cache_service (populated by a scheduler)
# -------------------------------------------------------------------
@router.get("/trending", response_model=List[str])
async def trending_posts(n: int = Query(10, ge=1, le=50)):
    trending = cache_service.get("trending_posts")
    return trending[:n] if trending else []


# -------------------------------------------------------------------
#  SINGLE POST  ← cached
# -------------------------------------------------------------------
@router.get("/{post_id}")
@cache(expire=POST_TTL)
async def get_single_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    publisher = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id == post.publisher_user_id
    ).first()
    return {
        "post_id": str(post.post_id),
        "publisher_user_id": str(post.publisher_user_id),
        "publisher_handle": publisher.user_handle if publisher else "Unknown",
        "publisher_avatar_path": publisher.avatar_path if publisher else None,
        "title": post.title, "description": post.description,
        "visual_image_path": post.visual_image_path,
        "categories": [{"category_id": l.category.category_id, "label": l.category.label} for l in post.category_links],
        "keywords": [{"keyword_id": l.keyword.keyword_id, "word": l.keyword.word} for l in post.keyword_links],
        "share_slug": post.share_slug, "share_url": f"/p/{post.share_slug}",
        "total_likes_count": post.total_likes_count,
        "total_comments_count": post.total_comments_count,
        "note": getattr(post, "note", None),
        "source_name": getattr(post, "source_name", None),
        "source_url": getattr(post, "source_url", None),
        "created_timestamp": post.created_timestamp,
    }


# -------------------------------------------------------------------
#  USER POSTS
# -------------------------------------------------------------------
@router.get("/user/{handle}")
async def get_user_posts(
    handle: str,
    skip: int = 0, limit: int = 50,
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
):
    user = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle == handle
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    posts = (
        posts_db.query(PostRecord)
        .filter(PostRecord.publisher_user_id == user.user_id)
        .order_by(PostRecord.created_timestamp.desc())
        .offset(skip).limit(limit).all()
    )
    return [
        {
            "post_id": str(p.post_id), "publisher_user_id": str(p.publisher_user_id),
            "publisher_handle": user.user_handle, "publisher_avatar_path": user.avatar_path,
            "title": p.title, "description": p.description,
            "visual_image_path": p.visual_image_path,
            "categories": [{"category_id": l.category.category_id, "label": l.category.label} for l in p.category_links],
            "keywords": [{"keyword_id": l.keyword.keyword_id, "word": l.keyword.word} for l in p.keyword_links],
            "total_likes_count": p.total_likes_count,
            "total_comments_count": p.total_comments_count,
            "created_timestamp": p.created_timestamp,
        }
        for p in posts
    ]


# -------------------------------------------------------------------
#  USER BOOKMARKS
# -------------------------------------------------------------------
@router.get("/user/{handle}/bookmarks")
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
    publisher_map = {
        u.user_id: u for u in
        users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id.in_(
                list(set(p.publisher_user_id for p in post_map.values()))
            )
        ).all()
    }

    results = []
    for bm in bookmarks:
        post = post_map.get(bm.post_id)
        if not post:
            continue
        pub = publisher_map.get(post.publisher_user_id)
        results.append({
            "post_id": str(post.post_id),
            "publisher_user_id": str(post.publisher_user_id),
            "publisher_handle": pub.user_handle if pub else "Unknown",
            "publisher_avatar_path": pub.avatar_path if pub else None,
            "title": post.title, "description": post.description,
            "visual_image_path": post.visual_image_path,
            "categories": [{"category_id": l.category.category_id, "label": l.category.label} for l in post.category_links],
            "keywords": [{"keyword_id": l.keyword.keyword_id, "word": l.keyword.word} for l in post.keyword_links],
            "share_slug": post.share_slug, "share_url": f"/p/{post.share_slug}",
            "total_likes_count": post.total_likes_count,
            "total_comments_count": post.total_comments_count,
            "note": getattr(post, "note", None),
            "source_name": getattr(post, "source_name", None),
            "source_url": getattr(post, "source_url", None),
            "created_timestamp": post.created_timestamp,
            "bookmarked_at": bm.created_timestamp,
        })
    return results


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
    return new_post


@router.put("/{post_id}", response_model=PostResponse)
async def update_post(
    post_id: uuid.UUID, payload: PostPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return await post_service.update_post(
        posts_db=posts_db, users_db=users_db,
        post_id=post_id, payload=payload, current_user=current_user,
    )


@router.delete("/{post_id}")
async def delete_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return await post_service.delete_post(
        posts_db=posts_db, post_id=post_id, current_user=current_user,
    )


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


@router.get("/reports/all", response_model=List[PostReportResponse])
async def get_reports(
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor", "support"])),
):
    return posts_db.query(PostReportRecord).all()


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
    return {"post_id": post_id, "user_id": current_user.user_id, "liked": liked, "total_likes_count": post.total_likes_count}


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
    comments_dict = {}
    for c in all_comments:
        commenter = commenter_map.get(c.user_id)
        comments_dict[c.comment_id] = {
            "comment_id": c.comment_id, "post_id": c.post_id, "user_id": c.user_id,
            "publisher_handle": commenter.user_handle if commenter else "Unknown",
            "publisher_avatar_path": commenter.avatar_path if commenter else None,
            "parent_comment_id": c.parent_comment_id, "content": c.content,
            "is_edited": bool(getattr(c, "is_edited", False)),
            "created_timestamp": c.created_timestamp, "replies": [],
        }
    top_level = []
    for c in all_comments:
        node = comments_dict[c.comment_id]
        if c.parent_comment_id and c.parent_comment_id in comments_dict:
            comments_dict[c.parent_comment_id]["replies"].append(node)
        else:
            top_level.append(node)
    return top_level