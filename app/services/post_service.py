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
        viewer=None,
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

        from app.services import access_service
        ds = self._dataset_raw(getattr(post, "dataset_id", None))
        spec = getattr(post, "visual_spec", None)
        if spec and ds is not None:
            if (ds.visibility or "public") == "private":
                spec = access_service.sign_spec(spec, ds.dataset_id)      # the browser may fetch the visual's data
            if (ds.pipeline or "batch") == "stream":
                spec = {**spec, "live": self._live(ds)}
        dataset = self._dataset_meta(posts_db, getattr(post, "dataset_id", None), viewer, ds)
        return {
            "post_id": str(post.post_id),
            "publisher_user_id": str(post.publisher_user_id),
            "publisher_handle": publisher.user_handle if publisher else "deleted_user",
            "publisher_avatar_path": publisher.avatar_path if publisher else None,
            "title": post.title,
            "description": post.description,
            "visual_image_path": post.visual_image_path,
            "dataset_id": getattr(post, "dataset_id", None),
            "visual_spec": spec,
            "theme_color": getattr(post, "theme_color", None),
            "dataset": dataset,
            "note": getattr(post, "note", None),
            # the source is the dataset's (older posts may still carry their own when the dataset has none)
            "source_name": (ds.source_name if ds is not None and ds.source_name else getattr(post, "source_name", None)),
            "source_url": (ds.source_url if ds is not None and ds.source_name else getattr(post, "source_url", None)),
            "categories": [link.category for link in post.category_links],
            "region": getattr(post, "region", None),
            "keywords": [link.keyword for link in post.keyword_links],
            "share_slug": post.share_slug,
            "share_url": f"/p/{post.share_slug}",
            "total_likes_count": post.total_likes_count,
            "total_comments_count": post.total_comments_count,
            "views_count": getattr(post, "views_count", 0) or 0,
            "backdrop": getattr(post, "backdrop", None),
            "is_active":post.is_active,
            "created_timestamp": post.created_timestamp,
            "updated_timestamp": getattr(post, "updated_timestamp", None),
            "is_liked": is_liked,
            "is_bookmarked": is_bookmarked,
            "visibility": getattr(post, "visibility", None) or "public",
            "post_type": getattr(post, "post_type", None) or "standard",
            "live": self._live(ds) if ds is not None and (ds.pipeline or "batch") == "stream" else None,
            "can_manage": access_service.can_manage_post(viewer, post) if viewer is not None else None,
        }

    @staticmethod
    def _dataset_raw(dataset_id: Optional[str]):
        if not dataset_id:
            return None
        from app.services import catalog_service
        return catalog_service.raw(dataset_id)

    @staticmethod
    def _live(ds) -> dict:
        return {"refresh_seconds": int(ds.refresh_seconds or 60),
                "updated_at": ds.last_ingested_at.isoformat() if ds.last_ingested_at else
                (ds.data_uploaded_at.isoformat() if ds.data_uploaded_at else None)}

    @staticmethod
    def _dataset_meta(posts_db: Session, dataset_id: Optional[str], viewer=None, d=None) -> Optional[dict]:
        """Catalog row for the dataset behind a post (shown on the post page). show_data = may THIS viewer see
        the rows (Data tab); a private dataset behind a public post shows its attribution, never its rows."""
        from app.services import access_service, licences
        if d is None:
            d = PostService._dataset_raw(dataset_id)
        if not d:
            return None
        v = viewer if viewer is not None else access_service.ANON
        return {
            "dataset_id": d.dataset_id, "title": d.title, "description": d.description,
            "category": d.category, "format": d.format, "source_name": d.source_name,
            "source_url": d.source_url, "license": d.license, "publisher": d.publisher,
            "geometry_type": d.geometry_type, "row_count": d.row_count, "crs": d.crs,
            "status": d.status, "updated_at": d.updated_timestamp.isoformat() if d.updated_timestamp else None,
            # when the data itself last changed: a stream's last row, a batch dataset's last upload (else the row)
            "data_updated_at": next((t.isoformat() for t in (d.last_ingested_at, d.data_uploaded_at, d.updated_timestamp) if t), None),
            "file_size_bytes": d.file_size_bytes,
            "show_data": access_service.can_see_rows(v, d),
            "visibility": d.visibility or "public", "pipeline": d.pipeline or "batch",
            "can_view": access_service.can_view_dataset(v, d),
            "licence": licences.summary(d.license_code), "license_code": d.license_code,
            "attribution": d.attribution, "copyright_holder": d.copyright_holder,
            "contains_personal_data": bool(d.contains_personal_data),
        }

    @staticmethod
    def _backdrop(posts_db, payload, previous: Optional[dict] = None) -> Optional[dict]:
        """The poster design (posts.backdrop): the publisher's design from the poster editor (a dict), or "auto"
        (colour, texture and title font picked from the topic), or "none". Nothing is drawn on the server."""
        from app.services import backdrop_service
        choice = getattr(payload, "backdrop", None)
        if choice is None and previous:
            return previous                                    # edit without a design: keep the poster as it is
        labels = [label for (label,) in posts_db.query(CategoryRecord.label)
                  .filter(CategoryRecord.category_id.in_(payload.category_ids or [-1])).all()]
        words = [k.strip().lower() for k in payload.keywords if k.strip()]
        try:
            return backdrop_service.for_post(payload.title, payload.description or "", labels, words, choice or "auto")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

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
            .filter(PostRecord.is_active == 1, PostRecord.visibility == "public")
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
    # Filtered feed (GET /posts/feed)
    # ------------------------------------------------------------------
    def feed(self, posts_db, users_db, *, category=None, region=None, visual_type=None, search=None,
             publisher=None, sort="trending", skip=0, limit=12, current_user_id=None) -> list:
        """Active posts filtered by category (slug), region (code), visual type (catalog code), text
        and publisher handle (region includes its sub-regions). sort: trending = most viewed, popular = most liked, recent = newest."""
        from sqlalchemy import or_
        q = posts_db.query(PostRecord).filter(PostRecord.is_active == 1, PostRecord.visibility == "public")
        if category:
            q = (q.join(PostCategoryLink, PostCategoryLink.post_id == PostRecord.post_id)
                  .join(CategoryRecord, CategoryRecord.category_id == PostCategoryLink.category_id)
                  .filter(CategoryRecord.slug == category))
        if region and region.strip().lower() != "global":   # Global = the whole world (no filter); a continent also matches its countries
            from app.services import region_service
            q = q.filter(PostRecord.region.in_(region_service.with_descendants(region)))
        if visual_type:
            q = q.filter(or_(PostRecord.visual_spec["visual_type"].astext == visual_type,
                             PostRecord.visual_spec["chart_type"].astext == visual_type))
        if search:
            like = f"%{search.strip()}%"
            kw_posts = (posts_db.query(PostKeywordLink.post_id)
                        .join(KeywordRecord, KeywordRecord.keyword_id == PostKeywordLink.keyword_id)
                        .filter(KeywordRecord.word.ilike(like)))
            q = q.filter(or_(PostRecord.title.ilike(like), PostRecord.description.ilike(like),
                             PostRecord.post_id.in_(kw_posts)))
        if publisher:
            user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == publisher).first()
            if user is None:
                return []
            q = q.filter(PostRecord.publisher_user_id == user.user_id)
        order = {
            "recent": [desc(PostRecord.created_timestamp)],
            "popular": [desc(PostRecord.total_likes_count), desc(PostRecord.created_timestamp)],
        }.get(sort, [desc(PostRecord.views_count), desc(PostRecord.total_likes_count), desc(PostRecord.created_timestamp)])
        posts = q.order_by(*order).offset(skip).limit(limit).all()
        return [self._format_post_response(posts_db, users_db, p, current_user_id) for p in posts]

   # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def _check_dataset_use(self, payload, viewer, allow_inactive: bool = False):
        """May this publisher build this post on this dataset, with this visibility? Returns the record.
        Private datasets: only for people who can see them; a PUBLIC post on a private dataset only for those
        who control the dataset (its rows stay hidden either way)."""
        from app.services import access_service
        payload.visibility = (payload.visibility or "public").strip().lower()
        if payload.visibility not in access_service.VISIBILITIES:
            raise HTTPException(status_code=422, detail="Visibility must be public or private")
        ds = self._dataset_raw(payload.dataset_id)
        if ds is None or (ds.status != "active" and not (allow_inactive and ds.columns)) \
                or not access_service.can_view_dataset(viewer, ds):
            raise HTTPException(status_code=404, detail="Dataset not found")
        if payload.visibility == "public" and not access_service.can_publish_publicly(viewer, ds):
            raise HTTPException(status_code=403, detail="This dataset is private and was shared with you: posts on it "
                                                         "must be private (share them with the people who need them)")
        want = (payload.post_type or "").strip().lower()
        kind = "live" if (ds.pipeline or "batch") == "stream" else "standard"
        if want and want != kind:
            raise HTTPException(status_code=422, detail="Live posts need a stream dataset; standard posts a batch dataset")
        payload.post_type = kind
        return ds

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
                color=getattr(vp, "color", None), story=vp.story() if vp else None,
            )
            data = visual_service.warm(payload.dataset_id, spec, allow_inactive)   # readers start on a cache hit
        except visual_service.VisualError as e:
            raise HTTPException(status_code=e.status, detail=e.message)
        except duckdb_service.StoreUnavailable:
            raise HTTPException(status_code=409, detail="This dataset's data has not been uploaded yet")
        except duckdb_service.EngineError as e:
            raise HTTPException(status_code=503 if e.status >= 500 else 400, detail=e.message)

        return spec, data

    def _render_image(self, posts_db, post, data=None) -> Optional[str]:
        """The post's poster as one square PNG (feed card, thumbnails, link previews): title, chart / map, legend,
        sources and the GISViz mark on the post's poster design. Never blocks saving: None when it fails."""
        from app.services import region_service
        try:
            spec = post.visual_spec or {}
            if data is None:
                data = visual_service.warm(post.dataset_id, spec, allow_inactive=True)
            labels = [label for (label,) in posts_db.query(CategoryRecord.label)
                      .join(PostCategoryLink, PostCategoryLink.category_id == CategoryRecord.category_id)
                      .filter(PostCategoryLink.post_id == post.post_id).all()]
            region = next((r for r in region_service.list_regions(include_inactive=True) if r["code"] == post.region), None)
            place = "Worldwide" if not region or region.get("kind") == "global" else region["name"]
            card = {}
            try:
                card = visual_service.get_card(post.dataset_id, True)
            except Exception:
                pass
            sources = []
            if card.get("source_name") or post.source_name:
                sources.append(card.get("source_name") or post.source_name)
            if post.source_name and card.get("source_name") and post.source_name != card["source_name"]:
                sources.append(post.source_name)
            if card.get("attribution"):
                sources.append(card["attribution"])
            elif card.get("title"):
                sources.append(card["title"] + (f", {card['license']}" if card.get("license") else ""))
            return visual_image.render_poster(
                title=spec.get("title") or post.title, subtitle=spec.get("subtitle") or "",
                eyebrow=" · ".join(x for x in (labels[0] if labels else "", place) if x),
                spec=spec, payload=data, design=post.backdrop, accent=post.theme_color or spec.get("accent"),
                sources=sources, note=post.note or "",
            )
        except Exception as exc:   # a preview glitch must not block publishing
            print(f"[post_service] poster image failed for post {post.post_id}: {exc}")
            return None

    def refresh_live_images(self, dataset_id: str, min_age_seconds: int = 600, limit: int = 3) -> None:
        """After rows were pushed to a stream dataset: redraw the poster images (feed / share) of its live posts,
        in the background, at most once per min_age_seconds per dataset."""
        import threading
        from app.services.cache_service import cache_service
        if os.getenv("LIVE_IMAGE_REFRESH", "1") != "1":
            return
        if cache_service.hit(f"gisviz:liveimg:{dataset_id}", min_age_seconds) > 1:
            return

        def run():
            from app.db.database import PostsSessionLocal
            with PostsSessionLocal() as db:
                posts = (db.query(PostRecord).filter(PostRecord.dataset_id == dataset_id, PostRecord.is_active == 1,
                                                     PostRecord.post_type == "live")
                         .order_by(PostRecord.views_count.desc()).limit(limit).all())
                for post in posts:
                    path = self._render_image(db, post)
                    if path:
                        old, post.visual_image_path = post.visual_image_path, path
                        db.commit()
                        self._delete_preview(old)
        threading.Thread(target=run, daemon=True).start()

    def repair_missing_images(self, limit: int = 200) -> dict:
        """Posts whose image file is gone (lost uploads folder, a file deleted by hand) get it drawn again, so feed
        cards, profile cards and share previews stop asking for a file that is not there (404)."""
        from app.db.database import PostsSessionLocal
        fixed = failed = 0
        with PostsSessionLocal() as db:
            posts = (db.query(PostRecord).filter(PostRecord.dataset_id.isnot(None))
                     .order_by(PostRecord.created_timestamp.desc()).all())
            missing = [p for p in posts
                       if not p.visual_image_path or (p.visual_image_path.startswith("/uploads/")
                                                      and not os.path.exists(p.visual_image_path.lstrip("/")))]
            for post in missing[:limit]:
                path = self._render_image(db, post)
                if path:
                    post.visual_image_path = path
                    db.commit()
                    fixed += 1
                else:
                    failed += 1
        return {"missing": len(missing), "fixed": fixed, "failed": failed}

    def rerender_images(self, posts_db) -> dict:
        """Admin: draw every active post's poster image again (after a design change of the image itself)."""
        done = failed = 0
        for post in posts_db.query(PostRecord).filter(PostRecord.dataset_id.isnot(None)).all():
            path = self._render_image(posts_db, post)
            if path:
                old, post.visual_image_path = post.visual_image_path, path
                posts_db.commit()
                self._delete_preview(old)
                done += 1
            else:
                failed += 1
        return {"rendered": done, "failed": failed}

    @staticmethod
    def _check_taxonomy(posts_db, payload) -> None:
        """Every post has a region (misc DB) and one or two categories (posts DB)."""
        from app.services import region_service
        payload.region = (payload.region or "").strip().lower() or None
        if not payload.region:
            raise HTTPException(status_code=422, detail="Pick the region this post is about")
        if not region_service.exists(payload.region):
            raise HTTPException(status_code=422, detail=f"Unknown region '{payload.region}'")
        ids = list(dict.fromkeys(payload.category_ids or []))
        if not ids:
            raise HTTPException(status_code=422, detail="Pick at least one category")
        if len(ids) > 2:
            raise HTTPException(status_code=422, detail="Pick at most two categories")
        found = {c for (c,) in posts_db.query(CategoryRecord.category_id).filter(CategoryRecord.category_id.in_(ids)).all()}
        if found != set(ids):
            raise HTTPException(status_code=422, detail="Unknown category")
        payload.category_ids = ids

    async def create_post(self, posts_db, users_db, user_id, payload, viewer=None):
        from app.services import access_service
        if not payload.dataset_id:
            raise HTTPException(status_code=422, detail="Pick a dataset for this post")
        self._check_taxonomy(posts_db, payload)
        dataset_id = payload.dataset_id.strip().lower()
        payload.dataset_id = dataset_id
        if viewer is None:
            viewer = access_service.viewer_for(users_db.get(PlatformUserRecord, user_id), users_db)
        self._check_dataset_use(payload, viewer)
        spec, data = self._prepare_dataset_visual(payload)

        new_post = PostRecord(
            publisher_user_id=user_id,
            title=payload.title,
            description=payload.description,
            dataset_id=dataset_id,
            visual_spec=spec,
            theme_color=spec.get("accent"),
            note=payload.note,
            source_name=None,               # the source comes from the dataset (see _format_post_response)
            source_url=None,
            region=payload.region,
            visibility=payload.visibility,
            post_type=payload.post_type,
            backdrop=self._backdrop(posts_db, payload),
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

        posts_db.flush()
        new_post.visual_image_path = self._render_image(posts_db, new_post, data)   # the poster, with its categories

        # Commit once, after all categories and keywords. (Previously this sat
        # inside the keyword loop, so only the first keyword was saved.)
        posts_db.commit()
        posts_db.refresh(new_post)
        return self._format_post_response(posts_db, users_db, new_post, user_id, viewer)

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

        self._check_taxonomy(posts_db, payload)
        # dataset rules are checked for the post's publisher (an admin may do anything)
        from app.services import access_service
        role = current_user.role.name if current_user.role else "viewer"
        owner = current_user if (post.publisher_user_id == current_user.user_id or role == "admin") \
            else users_db.get(PlatformUserRecord, post.publisher_user_id)
        viewer = access_service.viewer_for(owner, users_db) if owner is not None else access_service.ANON
        if payload.visibility is None:
            payload.visibility = post.visibility or "public"
        check = payload.model_copy(update={"dataset_id": (payload.dataset_id or post.dataset_id or "").strip().lower()})
        if check.dataset_id:
            self._check_dataset_use(check, viewer, allow_inactive=(check.dataset_id == post.dataset_id))
            post.post_type = check.post_type
        post.visibility = check.visibility
        payload.post_type = check.post_type
        post.region = payload.region
        post.title = payload.title
        post.description = payload.description
        post.note = payload.note
        if (payload.dataset_id or post.dataset_id) and self._dataset_raw(payload.dataset_id or post.dataset_id) is not None:
            post.source_name = post.source_url = None       # linked to the dataset's source from now on

        # Changing the dataset, chart or theme colour rebuilds the spec; the poster image is redrawn on every save.
        data = None
        if payload.dataset_id:
            payload.dataset_id = payload.dataset_id.strip().lower()
            # a post may keep (and re-chart) its own dataset even after that dataset was deactivated
            spec, data = self._prepare_dataset_visual(
                payload, allow_inactive=(payload.dataset_id == post.dataset_id))
            post.dataset_id = payload.dataset_id
            post.visual_spec = spec
            post.theme_color = spec.get("accent")

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

        post.backdrop = self._backdrop(posts_db, payload, getattr(post, "backdrop", None))
        posts_db.flush()
        # the poster image follows every edit (title, design, categories, chart)
        if post.dataset_id:
            image_path = self._render_image(posts_db, post, data)
            if image_path:
                old_image, post.visual_image_path = post.visual_image_path, image_path
                self._delete_preview(old_image)
        posts_db.commit()
        posts_db.refresh(post)
        return self._format_post_response(posts_db, users_db, post, current_user.user_id,
                                          access_service.viewer_for(current_user, users_db))

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