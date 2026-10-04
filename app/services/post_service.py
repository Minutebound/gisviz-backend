import os
import uuid
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException

from app.db.models import (
    PostRecord, PostCategoryLink, PostKeywordLink,
    CategoryRecord, KeywordRecord, PlatformUserRecord,
    PostLikeRecord, PostBookmarkRecord, DatasetRecord,
)
from app.schemas.post_schema import PostPayload
from app.services.helpers import generate_unique_slug
from app.services import visual_service, visual_image, duckdb_service


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
            "dataset_id": getattr(post, "dataset_id", None),
            "visual_spec": getattr(post, "visual_spec", None),
            "theme_color": getattr(post, "theme_color", None),
            "dataset": self._dataset_meta(posts_db, getattr(post, "dataset_id", None)),
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

    @staticmethod
    def _dataset_meta(posts_db: Session, dataset_id: Optional[str]) -> Optional[dict]:
        """Catalog row for the dataset behind a post (shown on the post page)."""
        if not dataset_id:
            return None
        from app.db.database import DatasetsSessionLocal      # catalog lives in the datasets DB
        with DatasetsSessionLocal() as ddb:
            d = ddb.get(DatasetRecord, dataset_id)
            if not d:
                return None
            return {
                "dataset_id": d.dataset_id, "title": d.title, "description": d.description,
                "category": d.category, "format": d.format, "source_name": d.source_name,
                "source_url": d.source_url, "license": d.license, "publisher": d.publisher,
                "geometry_type": d.geometry_type, "row_count": d.row_count, "crs": d.crs,
                "status": d.status, "updated_at": d.updated_timestamp.isoformat() if d.updated_timestamp else None,
                "file_size_bytes": d.file_size_bytes, "show_data": bool(d.show_data),
            }

    @staticmethod
    def _delete_preview(path: Optional[str]) -> None:
        """Remove a generated preview PNG we no longer need. Only touches uploads/visuals/post_*."""
        if not path or not path.startswith("/uploads/visuals/post_"):
            return
        try:
            os.remove(path.lstrip("/"))
        except OSError:
            pass

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
    def _prepare_dataset_visual(self, payload, allow_inactive: bool = False):
        """Validate the dataset choice, build the spec, warm the Redis payload cache and
        render the preview PNG from that same payload (one DuckDB query in total)."""
        vp = payload.visual_params
        try:
            spec = visual_service.build_spec(
                payload.dataset_id,
                getattr(vp, "viz", None), getattr(vp, "x", None),
                getattr(vp, "y", None), getattr(vp, "label_field", None),
                accent=payload.theme_color,
                z=getattr(vp, "z", None), size=getattr(vp, "size", None),
                allow_inactive=allow_inactive,
            )
            data = visual_service.warm(payload.dataset_id, spec, allow_inactive)   # readers start on a cache hit
        except visual_service.VisualError as e:
            raise HTTPException(status_code=e.status, detail=e.message)
        except duckdb_service.StoreUnavailable:
            raise HTTPException(status_code=409, detail="This dataset's data has not been uploaded yet")
        except duckdb_service.EngineError as e:
            raise HTTPException(status_code=503 if e.status >= 500 else 400, detail=e.message)

        image_path = None
        try:
            card = visual_service.get_card(payload.dataset_id, allow_inactive)
            image_path = visual_image.render_thumbnail(
                title=payload.title, subtitle=f"{card['title']} · {card.get('source_name') or ''}".strip(" ·"),
                spec=spec, payload=data, accent=spec.get("accent"),
            )
        except Exception as exc:   # a preview glitch must not block publishing
            print(f"[post_service] thumbnail render failed for {payload.dataset_id}: {exc}")
        return spec, image_path

    async def create_post(self, posts_db, users_db, user_id, payload):
        if not payload.dataset_id:
            raise HTTPException(status_code=422, detail="Pick a dataset for this post")
        dataset_id = payload.dataset_id.strip().lower()
        payload.dataset_id = dataset_id
        spec, image_path = self._prepare_dataset_visual(payload)

        new_post = PostRecord(
            publisher_user_id=user_id,
            title=payload.title,
            description=payload.description,
            visual_image_path=image_path,
            dataset_id=dataset_id,
            visual_spec=spec,
            theme_color=spec.get("accent"),
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

        # Commit once, after all categories and keywords. (Previously this sat
        # inside the keyword loop, so only the first keyword was saved.)
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

        # Changing the dataset, chart or theme colour rebuilds the spec and the preview PNG.
        if payload.dataset_id:
            payload.dataset_id = payload.dataset_id.strip().lower()
            # a post may keep (and re-chart) its own dataset even after that dataset was deactivated
            spec, image_path = self._prepare_dataset_visual(
                payload, allow_inactive=(payload.dataset_id == post.dataset_id))
            old_image = post.visual_image_path
            post.dataset_id = payload.dataset_id
            post.visual_spec = spec
            post.theme_color = spec.get("accent")
            if image_path:
                post.visual_image_path = image_path
                self._delete_preview(old_image)

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