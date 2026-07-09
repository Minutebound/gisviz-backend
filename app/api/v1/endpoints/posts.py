import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi_cache.decorator import cache
from sqlalchemy.orm import Session
from sqlalchemy import desc

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
POST_TTL   = 60
SEARCH_TTL = 60


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
#  SEARCH
# -------------------------------------------------------------------
@router.get("/search", response_model=List[PostResponse])
@cache(expire=SEARCH_TTL)
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
#  TRENDING
# -------------------------------------------------------------------
@router.get("/trending", response_model=List[str])
async def trending_posts(n: int = Query(10, ge=1, le=50)):
    trending = cache_service.get("trending_posts")
    return trending[:n] if trending else []




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
    posts = (
        posts_db.query(PostRecord)
        .filter(PostRecord.publisher_user_id == user.user_id)
        .order_by(PostRecord.created_timestamp.desc())
        .offset(skip).limit(limit).all()
    )
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
async def get_single_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
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
            "publisher_handle": commenter.user_handle if commenter else "deleted_user",
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