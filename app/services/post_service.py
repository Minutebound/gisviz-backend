import uuid
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException

from app.db.models import (
    PostRecord, PostCategoryLink, PostKeywordLink, CategoryRecord, KeywordRecord, PlatformUserRecord
)
from app.schemas.post_schema import PostPayload
from app.services.helpers import generate_unique_slug

class PostService:
    def _format_post_response(self, db: Session, post: PostRecord) -> dict:
        """Helper to serialize a PostRecord into a Pydantic-compliant dictionary."""
        publisher = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == post.publisher_user_id).first()
        return {
            "post_id": str(post.post_id),
            "publisher_user_id": str(post.publisher_user_id),
            "publisher_handle": publisher.user_handle if publisher else "Unknown",
            "publisher_avatar_path": publisher.avatar_path if publisher else None,
            "title": post.title,
            "description": post.description,
            "visual_image_path": post.visual_image_path,
            "note": getattr(post, 'note', None),
            "source_name": getattr(post, 'source_name', None),
            "source_url": getattr(post, 'source_url', None),
            "categories": [link.category for link in post.category_links],
            "keywords": [link.keyword for link in post.keyword_links],
            "share_slug": post.share_slug,
            "share_url": f"/p/{post.share_slug}",
            "total_likes_count": post.total_likes_count,
            "total_comments_count": post.total_comments_count,
            "created_timestamp": post.created_timestamp,
            "updated_timestamp": post.updated_timestamp
        }

    def retrieve_global_stream(self, posts_db: Session, users_db: Session, skip: int = 0, limit: int = 50) -> List[dict]:
        posts = posts_db.query(PostRecord).order_by(desc(PostRecord.created_timestamp)).offset(skip).limit(limit).all()
        return [self._format_post_response(users_db, p) for p in posts]

    def create_post(self, db: Session, user_id: uuid.UUID, payload: PostPayload) -> dict:
        new_post = PostRecord(
            publisher_user_id=user_id,
            title=payload.title,
            description=payload.description,
            visual_image_path=payload.visual_image_path,
            note=payload.note,
            source_name=payload.source_name,
            source_url=payload.source_url,
            share_slug=generate_unique_slug(db)
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
        
        return self._format_post_response(db, new_post)

    def update_post(self, db: Session, post_id: uuid.UUID, payload: PostPayload, current_user: PlatformUserRecord) -> dict:
        post = db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
        
        # RBAC Check: Must be the owner OR an admin/editor
        if post.publisher_user_id != current_user.user_id and current_user.role.name not in ["admin", "editor"]:
            raise HTTPException(status_code=403, detail="Not authorized to update this post")

        # Update scalar fields
        post.title = payload.title
        post.description = payload.description
        post.note = payload.note
        post.source_name = payload.source_name
        post.source_url = payload.source_url
        
        if payload.visual_image_path:
            post.visual_image_path = payload.visual_image_path

        # Clear existing category links and re-insert
        db.query(PostCategoryLink).filter(PostCategoryLink.post_id == post.post_id).delete()
        for cat_id in payload.category_ids:
            if db.query(CategoryRecord).filter(CategoryRecord.category_id == cat_id).first():
                db.add(PostCategoryLink(post_id=post.post_id, category_id=cat_id))

        # Clear existing keyword links and re-insert
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
        
        return self._format_post_response(db, post)

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