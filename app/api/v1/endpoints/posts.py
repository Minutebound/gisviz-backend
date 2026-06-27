import uuid
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List

from app.db.database import get_posts_db, get_users_db
from app.db.models import PlatformUserRecord, PostRecord, PostLikeRecord, PostCommentRecord, PostReportRecord, RoleRecord
from app.schemas.post_schema import (
    PostResponse,
    PostPayload,
    LikeResponse,
    CommentPayload,
    CommentData,
    PostReportPayload,
    PostReportResponse
)
from app.services.post_service import post_service
from app.services.cache_service import cache_service
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()

# -------------------------------------------------------------------
#  POST FEEDS & SEARCH
# -------------------------------------------------------------------

@router.get("/stream", response_model=List[PostResponse])
def fetch_post_stream(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    return post_service.retrieve_global_stream(
        posts_db=posts_db, users_db=users_db, skip=skip, limit=limit
    )

@router.get("/search", response_model=List[PostResponse])
def search_post_stream(
    q: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    # Basic text search on title or description
    posts = posts_db.query(PostRecord).filter(
        (PostRecord.title.ilike(f"%{q}%")) | (PostRecord.description.ilike(f"%{q}%"))
    ).order_by(desc(PostRecord.created_timestamp)).offset(skip).limit(limit).all()

    feed = []
    for p in posts:
        publisher = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == p.publisher_user_id).first()
        feed.append({
            "post_id": p.post_id,
            "publisher_user_id": p.publisher_user_id,
            "publisher_handle": publisher.user_handle if publisher else "Unknown",
            "publisher_avatar_path": publisher.avatar_path if publisher else None,
            "title": p.title,
            "description": p.description,
            "visual_image_path": p.visual_image_path,
            "categories": [link.category for link in p.category_links],
            "keywords": [link.keyword for link in p.keyword_links],
            "share_slug": p.share_slug,
            "share_url": f"/p/{p.share_slug}",
            "total_likes_count": p.total_likes_count,
            "total_comments_count": p.total_comments_count,
            "created_timestamp": p.created_timestamp
        })
    return feed

@router.get("/trending", response_model=List[str])
def trending_posts(n: int = Query(10, ge=1, le=50)):
    trending = cache_service.get("trending_posts")
    # Return cache if available, else empty list until cache is populated
    return trending[:n] if trending else []

# --- NEW: Get Single Post Endpoint ---
@router.get("/{post_id}")
def get_single_post(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db)
):
    """Fetch a single post by its ID to render the dedicated post page."""
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    publisher = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == post.publisher_user_id).first()
    
    return {
        "post_id": str(post.post_id),
        "publisher_user_id": str(post.publisher_user_id),
        "publisher_handle": publisher.user_handle if publisher else "Unknown",
        "publisher_avatar_path": publisher.avatar_path if publisher else None,
        "title": post.title,
        "description": post.description,
        "visual_image_path": post.visual_image_path,
        "categories": [{"category_id": link.category.category_id, "label": link.category.label} for link in post.category_links],
        "keywords": [{"keyword_id": link.keyword.keyword_id, "word": link.keyword.word} for link in post.keyword_links],
        "share_slug": post.share_slug,
        "share_url": f"/p/{post.share_slug}",
        "total_likes_count": post.total_likes_count,
        "total_comments_count": post.total_comments_count,
        "created_timestamp": post.created_timestamp
    }


# -------------------------------------------------------------------
#  POST CRUD (Auto-Upgrade RBAC applied)
# -------------------------------------------------------------------

@router.post("", response_model=PostResponse, status_code=201)
def create_post(
    payload: PostPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    # Swapped RoleChecker for base authentication so Viewers can hit this route
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    # 1. Create the Post via Service
    new_post = post_service.create_post(
        db=posts_db, 
        user_id=current_user.user_id, 
        payload=payload
    )

    # 2. Update User Stats & Apply the Role Upgrade Rule
    current_user.post_count = (current_user.post_count or 0) + 1
    
    # If they are currently a viewer, upgrade them to a publisher
    if current_user.role and current_user.role.name == "viewer":
        publisher_role = users_db.query(RoleRecord).filter(RoleRecord.name == "publisher").first()
        if publisher_role:
            current_user.role_id = publisher_role.role_id
            if not current_user.title:
                current_user.title = "Platform Publisher"
                
    users_db.commit()

    return {
        "post_id": new_post.post_id,
        "publisher_user_id": current_user.user_id,
        "publisher_handle": current_user.user_handle,
        "publisher_avatar_path": current_user.avatar_path,
        "title": new_post.title,
        "description": new_post.description,
        "visual_image_path": new_post.visual_image_path,
        "categories": [link.category for link in new_post.category_links],
        "keywords": [link.keyword for link in new_post.keyword_links],
        "share_slug": new_post.share_slug,
        "share_url": f"/p/{new_post.share_slug}",
        "total_likes_count": new_post.total_likes_count,
        "total_comments_count": new_post.total_comments_count,
        "created_timestamp": new_post.created_timestamp
    }

@router.put("/{post_id}", response_model=PostResponse)
def update_post(
    post_id: uuid.UUID,
    payload: PostPayload,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    updated_post = post_service.update_post(db=db, post_id=post_id, payload=payload, current_user=current_user)
    
    # We return the mapped schema back to the frontend
    return {
        "post_id": updated_post.post_id,
        "publisher_user_id": updated_post.publisher_user_id,
        "publisher_handle": current_user.user_handle,
        "publisher_avatar_path": current_user.avatar_path,
        "title": updated_post.title,
        "description": updated_post.description,
        "visual_image_path": updated_post.visual_image_path,
        "categories": [link.category for link in updated_post.category_links],
        "keywords": [link.keyword for link in updated_post.keyword_links],
        "share_slug": updated_post.share_slug,
        "share_url": f"/p/{updated_post.share_slug}",
        "total_likes_count": updated_post.total_likes_count,
        "total_comments_count": updated_post.total_comments_count,
        "created_timestamp": updated_post.created_timestamp
    }

@router.delete("/{post_id}")
def delete_post(
    post_id: uuid.UUID,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    # The post_service.delete_post method handles the RBAC check to ensure
    # only the owner OR an admin/editor can delete it.
    return post_service.delete_post(db=db, post_id=post_id, current_user=current_user)


# -------------------------------------------------------------------
#  ENGAGEMENT: LIKES & COMMENTS
# -------------------------------------------------------------------

@router.post("/{post_id}/like", response_model=LikeResponse)
def toggle_like(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    existing_like = posts_db.query(PostLikeRecord).filter(
        PostLikeRecord.post_id == post_id,
        PostLikeRecord.user_id == current_user.user_id
    ).first()
    
    if existing_like:
        posts_db.delete(existing_like)
        post.total_likes_count = max(0, post.total_likes_count - 1)
        liked = False
    else:
        posts_db.add(PostLikeRecord(post_id=post_id, user_id=current_user.user_id))
        post.total_likes_count += 1
        liked = True
        
    posts_db.commit()
    return {"post_id": post_id, "user_id": current_user.user_id, "liked": liked, "total_likes_count": post.total_likes_count}


@router.post("/{post_id}/comments", response_model=CommentData, status_code=201)
def add_comment(
    post_id: uuid.UUID,
    payload: CommentPayload,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    new_comment = PostCommentRecord(
        post_id=post_id,
        user_id=current_user.user_id,
        parent_comment_id=payload.parent_comment_id,
        content=payload.content
    )
    posts_db.add(new_comment)
    post.total_comments_count += 1
    posts_db.commit()
    posts_db.refresh(new_comment)

    return CommentData(
        comment_id=new_comment.comment_id,
        post_id=new_comment.post_id,
        user_id=new_comment.user_id,
        publisher_handle=current_user.user_handle,
        publisher_avatar_path=current_user.avatar_path,
        parent_comment_id=new_comment.parent_comment_id,
        content=new_comment.content,
        is_edited=False,
        created_timestamp=new_comment.created_timestamp,
        replies=[],
    )


@router.get("/{post_id}/comments", response_model=List[CommentData])
def get_comments(
    post_id: uuid.UUID,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    # 1. Fetch ALL comments for this post in a single query
    all_comments = posts_db.query(PostCommentRecord).filter(
        PostCommentRecord.post_id == post_id
    ).order_by(PostCommentRecord.created_timestamp.asc()).all()

    # 2. Fetch all associated users in a single query to avoid performance hits
    user_ids = list(set([c.user_id for c in all_comments]))
    users = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id.in_(user_ids)
    ).all()
    user_map = {u.user_id: u for u in users}

    # 3. Convert database records to dictionaries
    comments_dict = {}
    for c in all_comments:
        publisher = user_map.get(c.user_id)
        comments_dict[c.comment_id] = {
            "comment_id": c.comment_id,
            "post_id": c.post_id,
            "user_id": c.user_id,
            "publisher_handle": publisher.user_handle if publisher else "Unknown",
            "publisher_avatar_path": publisher.avatar_path if publisher else None,
            "parent_comment_id": c.parent_comment_id,
            "content": c.content,
            "is_edited": bool(c.is_edited),
            "created_timestamp": c.created_timestamp,
            "updated_timestamp": c.updated_timestamp,
            "replies": []
        }

    # 4. Assemble the hierarchical reply tree in Python
    top_level_comments = []
    for c in all_comments:
        comment_data = comments_dict[c.comment_id]
        
        # If it has a parent, attach it to the parent's "replies" array
        if c.parent_comment_id and c.parent_comment_id in comments_dict:
            comments_dict[c.parent_comment_id]["replies"].append(comment_data)
        # Otherwise, it's a top-level comment
        else:
            top_level_comments.append(comment_data)

    return top_level_comments


# -------------------------------------------------------------------
#  MODERATION & REPORTS
# -------------------------------------------------------------------

@router.post("/{post_id}/report", response_model=PostReportResponse, status_code=201)
def report_post(
    post_id: uuid.UUID,
    payload: PostReportPayload,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    report = PostReportRecord(
        post_id=post_id,
        reporter_user_id=current_user.user_id,
        reason=payload.reason
    )
    posts_db.add(report)
    posts_db.commit()
    posts_db.refresh(report)
    return report

@router.get("/reports/all", response_model=List[PostReportResponse])
def get_reports(
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor", "support"]))
):
    return posts_db.query(PostReportRecord).all()

@router.get("/user/{handle}")
def get_user_posts(
    handle: str, 
    skip: int = 0, 
    limit: int = 50, 
    users_db: Session = Depends(get_users_db), 
    posts_db: Session = Depends(get_posts_db)
):
    """Fetch the specific post feed for a given user handle."""
    user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    posts = (
        posts_db.query(PostRecord)
        .filter(PostRecord.publisher_user_id == user.user_id)
        .order_by(PostRecord.created_timestamp.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    
    results = []
    for p in posts:
        results.append({
            "post_id": str(p.post_id),
            "publisher_user_id": str(p.publisher_user_id),
            "publisher_handle": user.user_handle,
            "publisher_avatar_path": user.avatar_path,
            "title": p.title,
            "description": p.description,
            "visual_image_path": p.visual_image_path,
            "categories": [{"category_id": link.category.category_id, "label": link.category.label} for link in p.category_links],
            "keywords": [{"keyword_id": link.keyword.keyword_id, "word": link.keyword.word} for link in p.keyword_links],
            "total_likes_count": p.total_likes_count,
            "total_comments_count": p.total_comments_count,
            "created_timestamp": p.created_timestamp
        })
    return results