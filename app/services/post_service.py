import uuid
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException

from app.db.models import (
    PostRecord, PostCategoryLink, PostKeywordLink,
    CategoryRecord, KeywordRecord, PlatformUserRecord
)
from app.schemas.post_schema import PostPayload
from app.services.helpers import generate_unique_slug


class PostService:

    # ------------------------------------------------------------------
    # Helper: serialise a PostRecord -> dict
    #
    # IMPORTANT: posts_db and users_db must NEVER be swapped.
    #   posts_db  -> PostsBase session (PostRecord, CategoryRecord, etc.)
    #   users_db  -> UsersBase session (PlatformUserRecord)
    #
    # platform_users only exists in the users database. Querying it via
    # a posts_db session causes the 500 "relation does not exist" error.
    # ------------------------------------------------------------------
    def _format_post_response(
        self,
        posts_db: Session,
        users_db: Session,
        post: PostRecord,
    ) -> dict:
        publisher = users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id == post.publisher_user_id
        ).first()

        return {
            "post_id": str(post.post_id),
            "publisher_user_id": str(post.publisher_user_id),
            "publisher_handle": publisher.user_handle if publisher else "Unknown",
            "publisher_avatar_path": publisher.avatar_path if publisher else None,
            "title": post.title,
            "description": post.description,
            "visual_image_path": post.visual_image_path,
            "note": getattr(post, "note", None),
            "source_name": getattr(post, "source_name", None),
            "source_url": getattr(post, "source_url", None),
            "categories": [link.category for link in post.category_links],
            "keywords": [link.keyword for link in post.keyword_links],
            "share_slug": post.share_slug,
            "share_url": f"/p/{post.share_slug}",
            "total_likes_count": post.total_likes_count,
            "total_comments_count": post.total_comments_count,
            "created_timestamp": post.created_timestamp,
            "updated_timestamp": getattr(post, "updated_timestamp", None),
        }

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------
    def retrieve_global_stream(
        self,
        posts_db: Session,
        users_db: Session,
        skip: int = 0,
        limit: int = 50,
    ) -> List[dict]:
        posts = (
            posts_db.query(PostRecord)
            .order_by(desc(PostRecord.created_timestamp))
            .offset(skip)
            .limit(limit)
            .all()
        )
        return [self._format_post_response(posts_db, users_db, p) for p in posts]

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_post(
        self,
        posts_db: Session,
        users_db: Session,
        user_id: uuid.UUID,
        payload: PostPayload,
    ) -> dict:
        new_post = PostRecord(
            publisher_user_id=user_id,
            title=payload.title,
            description=payload.description,
            visual_image_path=payload.visual_image_path,
            note=payload.note,
            source_name=payload.source_name,
            source_url=payload.source_url,
            share_slug=generate_unique_slug(posts_db),
        )
        posts_db.add(new_post)
        posts_db.flush()

        for cat_id in payload.category_ids:
            if posts_db.query(CategoryRecord).filter(
                CategoryRecord.category_id == cat_id
            ).first():
                posts_db.add(PostCategoryLink(post_id=new_post.post_id, category_id=cat_id))

        for kw_str in payload.keywords:
            clean_kw = kw_str.strip().lower()
            keyword_record = posts_db.query(KeywordRecord).filter(
                KeywordRecord.word == clean_kw
            ).first()
            if not keyword_record:
                keyword_record = KeywordRecord(word=clean_kw)
                posts_db.add(keyword_record)
                posts_db.flush()
            keyword_record.usage_count += 1
            posts_db.add(PostKeywordLink(
                post_id=new_post.post_id,
                keyword_id=keyword_record.keyword_id,
            ))

        posts_db.commit()
        posts_db.refresh(new_post)
        return self._format_post_response(posts_db, users_db, new_post)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update_post(
        self,
        posts_db: Session,
        users_db: Session,
        post_id: uuid.UUID,
        payload: PostPayload,
        current_user: PlatformUserRecord,
    ) -> dict:
        post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        if (
            post.publisher_user_id != current_user.user_id
            and current_user.role.name not in ["admin", "editor"]
        ):
            raise HTTPException(status_code=403, detail="Not authorized to update this post")

        post.title = payload.title
        post.description = payload.description
        post.note = payload.note
        post.source_name = payload.source_name
        post.source_url = payload.source_url

        if payload.visual_image_path:
            post.visual_image_path = payload.visual_image_path

        # Replace category links
        posts_db.query(PostCategoryLink).filter(
            PostCategoryLink.post_id == post.post_id
        ).delete()
        for cat_id in payload.category_ids:
            if posts_db.query(CategoryRecord).filter(
                CategoryRecord.category_id == cat_id
            ).first():
                posts_db.add(PostCategoryLink(post_id=post.post_id, category_id=cat_id))

        # Replace keyword links
        posts_db.query(PostKeywordLink).filter(
            PostKeywordLink.post_id == post.post_id
        ).delete()
        for kw_str in payload.keywords:
            clean_kw = kw_str.strip().lower()
            keyword_record = posts_db.query(KeywordRecord).filter(
                KeywordRecord.word == clean_kw
            ).first()
            if not keyword_record:
                keyword_record = KeywordRecord(word=clean_kw)
                posts_db.add(keyword_record)
                posts_db.flush()
            keyword_record.usage_count += 1
            posts_db.add(PostKeywordLink(
                post_id=post.post_id,
                keyword_id=keyword_record.keyword_id,
            ))

        posts_db.commit()
        posts_db.refresh(post)
        return self._format_post_response(posts_db, users_db, post)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------
    def delete_post(
        self,
        posts_db: Session,
        post_id: uuid.UUID,
        current_user: PlatformUserRecord,
    ):
        post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        if (
            post.publisher_user_id != current_user.user_id
            and current_user.role.name not in ["admin", "editor"]
        ):
            raise HTTPException(status_code=403, detail="Not authorized to delete this post")

        posts_db.delete(post)
        posts_db.commit()
        return {"status": "deleted"}


post_service = PostService()