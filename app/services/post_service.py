import uuid
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException

from app.db.models import (
    PostRecord, PostCategoryLink, PostKeywordLink, CategoryRecord, KeywordRecord, PlatformUserRecord
)
from app.schemas.post_schema import PostPayload

def _make_share_slug() -> str:
    return uuid.uuid4().hex[:8]

class PostService:
    def retrieve_global_stream(self, posts_db: Session, users_db: Session, skip: int = 0, limit: int = 50) -> List[dict]:
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.created_timestamp)).offset(skip).limit(limit).all()
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

    def create_post(self, db: Session, user_id: uuid.UUID, payload: PostPayload) -> PostRecord:
        new_post = PostRecord(
            publisher_user_id=user_id,
            title=payload.title,
            description=payload.description,
            visual_image_path=payload.visual_image_path,
            share_slug=_make_share_slug()
        )
        db.add(new_post)
        db.flush()

        for cat_id in payload.category_ids:
            if db.query(CategoryRecord).filter(CategoryRecord.category_id == cat_id).first():
                db.add(PostCategoryLink(post_id=new_post.post_id, category_id=cat_id))

        for kw_str in payload.keywords:
            clean_kw = kw_str.strip().lower()
            keyword_record = db.query(KeywordRecord).filter(KeywordRecord.word == clean_kw).first()
            if not keyword_record:
                keyword_record = KeywordRecord(word=clean_kw)
                db.add(keyword_record)
                db.flush()
            
            keyword_record.usage_count += 1
            db.add(PostKeywordLink(post_id=new_post.post_id, keyword_id=keyword_record.keyword_id))

        db.commit()
        db.refresh(new_post)
        return new_post

    def update_post(self, db: Session, post_id: uuid.UUID, payload: PostPayload, current_user: PlatformUserRecord) -> PostRecord:
        post = db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
        
        # RBAC Check: Must be the owner OR an admin/editor
        if post.publisher_user_id != current_user.user_id and current_user.role.name not in ["admin", "editor"]:
            raise HTTPException(status_code=403, detail="Not authorized to update this post")

        # Update scalar fields
        post.title = payload.title
        post.description = payload.description
        if payload.visual_image_path:
            post.visual_image_path = payload.visual_image_path

        # Clear existing category links and re-insert
        db.query(PostCategoryLink).filter(PostCategoryLink.post_id == post.post_id).delete()
        for cat_id in payload.category_ids:
            if db.query(CategoryRecord).filter(CategoryRecord.category_id == cat_id).first():
                db.add(PostCategoryLink(post_id=post.post_id, category_id=cat_id))

        # Clear existing keyword links and re-insert
        # Optional: You could decrement the usage_count of old keywords here for absolute precision
        db.query(PostKeywordLink).filter(PostKeywordLink.post_id == post.post_id).delete()
        for kw_str in payload.keywords:
            clean_kw = kw_str.strip().lower()
            keyword_record = db.query(KeywordRecord).filter(KeywordRecord.word == clean_kw).first()
            if not keyword_record:
                keyword_record = KeywordRecord(word=clean_kw)
                db.add(keyword_record)
                db.flush()
            
            keyword_record.usage_count += 1
            db.add(PostKeywordLink(post_id=post.post_id, keyword_id=keyword_record.keyword_id))

        db.commit()
        db.refresh(post)
        return post

    def delete_post(self, db: Session, post_id: uuid.UUID, current_user: PlatformUserRecord):
        post = db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
        
        if post.publisher_user_id != current_user.user_id and current_user.role.name not in ["admin", "editor"]:
            raise HTTPException(status_code=403, detail="Not authorized to delete this post")

        db.delete(post)
        db.commit()
        return {"status": "deleted"}

post_service = PostService()