from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.db.database import get_posts_db, get_users_db
from app.db.models import (
    PlatformUserRecord, PostRecord, 
    CategoryRecord, KeywordRecord
)

router = APIRouter()

# Notice the path is just "/global" because we will prefix the router with "/search"
@router.get("/global")
def global_search(
    q: str = Query(..., min_length=3),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db)
):
    """Unified search returning users, posts, categories, and tags."""
    
    # 1. Search Users
    users = users_db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle.ilike(f"%{q}%")
    ).limit(10).all()

    # 2. Search Posts
    posts = posts_db.query(PostRecord).filter(
        (PostRecord.title.ilike(f"%{q}%")) | (PostRecord.description.ilike(f"%{q}%"))
    ).order_by(desc(PostRecord.created_timestamp)).limit(10).all()

    # 3. Search Categories
    categories = posts_db.query(CategoryRecord).filter(
        CategoryRecord.label.ilike(f"%{q}%")
    ).limit(10).all()

    # 4. Search Tags
    tags = posts_db.query(KeywordRecord).filter(
        KeywordRecord.word.ilike(f"%{q}%")
    ).limit(15).all()

    return {
        "users": [
            {"user_id": str(u.user_id), "user_handle": u.user_handle, "avatar_path": u.avatar_path}
            for u in users
        ],
        "posts": [
            {"post_id": str(p.post_id), "title": p.title, "share_slug": p.share_slug, "visual_image_path": p.visual_image_path}
            for p in posts
        ],
        "categories": [
            {"category_id": str(c.category_id), "label": c.label}
            for c in categories
        ],
        "tags": [
            {"keyword_id": str(t.keyword_id), "word": t.word}
            for t in tags
        ]
    }