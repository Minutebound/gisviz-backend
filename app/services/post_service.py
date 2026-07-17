import uuid
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException

from app.db.models import (
    PostRecord, PostCategoryLink, PostKeywordLink,
    CategoryRecord, KeywordRecord, PlatformUserRecord,
    PostLikeRecord, PostBookmarkRecord,
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
    # current_user_id: when provided the dict includes is_liked and
    # is_bookmarked flags sourced from the DB for that user.
    # ------------------------------------------------------------------
    def _format_post_response(
        self,
        posts_db: Session,
        users_db: Session,
        post: PostRecord,
        current_user_id: Optional[uuid.UUID] = None,
    ) -> dict:
        publisher = users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id == post.publisher_user_id
        ).first()

        # Per-user interaction flags — None when not authenticated
        is_liked: Optional[bool] = None
        is_bookmarked: Optional[bool] = None
        if current_user_id is not None:
            is_liked = posts_db.query(PostLikeRecord).filter(
                PostLikeRecord.post_id == post.post_id,
                PostLikeRecord.user_id == current_user_id,
            ).first() is not None

            is_bookmarked = posts_db.query(PostBookmarkRecord).filter(
                PostBookmarkRecord.post_id == post.post_id,
                PostBookmarkRecord.user_id == current_user_id,
            ).first() is not None

        return {
            "post_id": str(post.post_id),
            "publisher_user_id": str(post.publisher_user_id),
            "publisher_handle": publisher.user_handle if publisher else "deleted_user",
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
            "is_active":post.is_active,
            "created_timestamp": post.created_timestamp,
            "updated_timestamp": getattr(post, "updated_timestamp", None),
            "is_liked": is_liked,
            "is_bookmarked": is_bookmarked,
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
        current_user_id: Optional[uuid.UUID] = None,
    ) -> List[dict]:
        posts = (
            posts_db.query(PostRecord)
            .filter(PostRecord.is_active == 1)
            .order_by(desc(PostRecord.created_timestamp))
            .offset(skip)
            .limit(limit)
            .all()
        )
        return [
            self._format_post_response(posts_db, users_db, p, current_user_id)
            for p in posts
        ]

   # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def create_post(self, posts_db, users_db, user_id, payload):
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
        posts_db.flush()  # get post_id without committing

        for cat_id in payload.category_ids:
            link = PostCategoryLink(post_id=new_post.post_id, category_id=cat_id)
            posts_db.add(link)
            # ← Increment category usage count
            cat = posts_db.query(CategoryRecord).filter(
                CategoryRecord.category_id == cat_id
            ).first()
            if cat:
                cat.usage_count = (cat.usage_count or 0) + 1

        for word in payload.keywords:
            word = word.strip().lower()
            if not word:
                continue
            kw = posts_db.query(KeywordRecord).filter(
                KeywordRecord.word == word
            ).first()
            if not kw:
                kw = KeywordRecord(word=word, usage_count=0)
                posts_db.add(kw)
                posts_db.flush()
            kw.usage_count = (kw.usage_count or 0) + 1
            posts_db.add(PostKeywordLink(post_id=new_post.post_id, keyword_id=kw.keyword_id))

            posts_db.commit()
            posts_db.refresh(new_post)
            return self._format_post_response(posts_db, users_db, new_post, user_id)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update_post(
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

        # FIX: Decrement old categories before deleting links
        old_cat_links = posts_db.query(PostCategoryLink).filter(
            PostCategoryLink.post_id == post.post_id
        ).all()
        for link in old_cat_links:
            cat = posts_db.query(CategoryRecord).filter(CategoryRecord.category_id == link.category_id).first()
            if cat and cat.usage_count > 0:
                cat.usage_count -= 1

        posts_db.query(PostCategoryLink).filter(
            PostCategoryLink.post_id == post.post_id
        ).delete()
        
        for cat_id in payload.category_ids:
            # FIX: Increment new categories
            cat = posts_db.query(CategoryRecord).filter(
                CategoryRecord.category_id == cat_id
            ).first()
            if cat:
                cat.usage_count += 1
                posts_db.add(PostCategoryLink(post_id=post.post_id, category_id=cat_id))

        # FIX: Decrement old keywords before deleting links
        old_kw_links = posts_db.query(PostKeywordLink).filter(
            PostKeywordLink.post_id == post.post_id
        ).all()
        for link in old_kw_links:
            kw = posts_db.query(KeywordRecord).filter(KeywordRecord.keyword_id == link.keyword_id).first()
            if kw and kw.usage_count > 0:
                kw.usage_count -= 1

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
        return self._format_post_response(posts_db, users_db, post, current_user.user_id)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------
    
    async def delete_post(self, posts_db, post_id, current_user, users_db=None):
        post = posts_db.query(PostRecord).filter(PostRecord.post_id == post_id).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        if (
            post.publisher_user_id != current_user.user_id
            and current_user.role.name not in ["admin", "editor"]
        ):
            raise HTTPException(status_code=403, detail="Not authorized to delete this post")

        # Decrement category usage counts
        for link in post.category_links:
            if link.category and link.category.usage_count > 0:
                link.category.usage_count -= 1

        # Decrement keyword usage counts
        for link in post.keyword_links:
            if link.keyword and link.keyword.usage_count > 0:
                link.keyword.usage_count -= 1

        posts_db.delete(post)
        posts_db.commit()

        # Decrement post_count on the user (users_db is a separate session)
        if users_db is not None:
            publisher = users_db.query(PlatformUserRecord).filter(
                PlatformUserRecord.user_id == post.publisher_user_id
            ).first()
            if publisher and publisher.post_count > 0:
                publisher.post_count -= 1
                users_db.commit()

        return {"status": "deleted"}


post_service = PostService()