import json
import secrets
from typing import List, Dict, Any, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import (
    GeographicPublicationRecord,
    PublicationLikeRecord,
    PublicationCommentRecord,
    CategoryRecord,
    PublicationCategoryLink,
    PlatformUserRecord,
)
from app.services.cache_service import cache_service
from app.core.config import settings


def _make_share_slug() -> str:
    return secrets.token_urlsafe(8)[:12]


class GeographicPublicationService:

    # ----------------------------------------------------------------
    # Shared: hydrate raw rows with publisher info + categories
    # ----------------------------------------------------------------
    def _hydrate(
        self,
        posts_db: Session,
        users_db: Session,
        rows: list,
    ) -> List[Dict[str, Any]]:
        if not rows:
            return []

        publication_ids = [str(r.publication_id) for r in rows]
        publisher_ids = list({str(r.publisher_user_id) for r in rows})

        # --- publisher profiles (users DB) ---
        users = (
            users_db.query(PlatformUserRecord)
            .filter(PlatformUserRecord.user_id.in_(publisher_ids))
            .all()
        )
        user_map = {str(u.user_id): u for u in users}

        # --- categories for these publications (posts DB) ---
        cat_rows = (
            posts_db.query(PublicationCategoryLink, CategoryRecord)
            .join(CategoryRecord, PublicationCategoryLink.category_id == CategoryRecord.category_id)
            .filter(PublicationCategoryLink.publication_id.in_(publication_ids))
            .all()
        )
        cat_map: Dict[str, List[Dict[str, Any]]] = {}
        for link, cat in cat_rows:
            cat_map.setdefault(str(link.publication_id), []).append(
                {
                    "category_id": cat.category_id,
                    "slug": cat.slug,
                    "label": cat.label,
                    "usage_count": cat.usage_count,
                }
            )

        formatted = []
        for row in rows:
            d = dict(row._mapping)
            if d.get("spatial_geometry"):
                d["spatial_geometry"] = json.loads(d["spatial_geometry"])

            publisher = user_map.get(str(d["publisher_user_id"]))
            d["publisher_handle"] = publisher.user_handle if publisher else "Unknown User"
            d["publisher_avatar_url"] = publisher.avatar_storage_url if publisher else ""
            d["categories"] = cat_map.get(str(d["publication_id"]), [])
            d["share_url"] = f"/p/{d['share_slug']}"
            formatted.append(d)

        return formatted

    def _select_sql(self, where: str = "", order: str = "ORDER BY created_timestamp DESC") -> str:
        return f"""
            SELECT
                publication_id, publisher_user_id, publication_title,
                layer_attribute_metadata, share_slug,
                total_likes_count, total_comments_count,
                created_timestamp, updated_timestamp,
                ST_AsGeoJSON(spatial_geometry) AS spatial_geometry
            FROM geographic_publications
            {where}
            {order}
            OFFSET :skip LIMIT :limit
        """

    # ----------------------------------------------------------------
    # Global stream
    # ----------------------------------------------------------------
    def retrieve_global_stream(
        self, posts_db: Session, users_db: Session, skip: int = 0, limit: int = 50
    ):
        rows = posts_db.execute(
            text(self._select_sql()), {"skip": skip, "limit": limit}
        ).fetchall()
        return self._hydrate(posts_db, users_db, rows)

    # ----------------------------------------------------------------
    # Search with read-through Redis cache
    # ----------------------------------------------------------------
    def search_publications(
        self, posts_db: Session, users_db: Session, query: str, skip: int = 0, limit: int = 50
    ):
        cached = cache_service.get_search_results(query, skip, limit)
        if cached is not None:
            return cached

        sql = self._select_sql(
            where="WHERE publication_title ILIKE :q",
            order="ORDER BY created_timestamp DESC",
        )
        rows = posts_db.execute(
            text(sql), {"q": f"%{query}%", "skip": skip, "limit": limit}
        ).fetchall()
        results = self._hydrate(posts_db, users_db, rows)

        cache_service.set_search_results(query, skip, limit, results)
        return results

    # ----------------------------------------------------------------
    # Likes (toggle) — keeps PG counter + Redis mirror in sync
    # ----------------------------------------------------------------
    def toggle_like(self, posts_db: Session, publication_id: str, user_id: str) -> Dict[str, Any]:
        pub = (
            posts_db.query(GeographicPublicationRecord)
            .filter(GeographicPublicationRecord.publication_id == publication_id)
            .first()
        )
        if not pub:
            raise ValueError("Publication not found")

        existing = (
            posts_db.query(PublicationLikeRecord)
            .filter(
                PublicationLikeRecord.publication_id == publication_id,
                PublicationLikeRecord.user_id == user_id,
            )
            .first()
        )

        if existing:
            posts_db.delete(existing)
            pub.total_likes_count = max(0, pub.total_likes_count - 1)
            liked = False
            cache_service.incr_like(str(publication_id), -1)
            cache_service.bump_trending(str(publication_id), weight=-1.0)
        else:
            posts_db.add(PublicationLikeRecord(publication_id=publication_id, user_id=user_id))
            pub.total_likes_count += 1
            liked = True
            cache_service.incr_like(str(publication_id), 1)
            cache_service.bump_trending(str(publication_id), weight=1.0)

        posts_db.commit()
        posts_db.refresh(pub)
        return {
            "publication_id": publication_id,
            "user_id": user_id,
            "liked": liked,
            "total_likes_count": pub.total_likes_count,
        }

    # ----------------------------------------------------------------
    # Comments
    # ----------------------------------------------------------------
    def add_comment(
        self, posts_db: Session, publication_id: str, user_id: str,
        content: str, parent_comment_id: Optional[str] = None,
    ) -> PublicationCommentRecord:
        pub = (
            posts_db.query(GeographicPublicationRecord)
            .filter(GeographicPublicationRecord.publication_id == publication_id)
            .first()
        )
        if not pub:
            raise ValueError("Publication not found")

        if parent_comment_id:
            parent = (
                posts_db.query(PublicationCommentRecord)
                .filter(
                    PublicationCommentRecord.comment_id == parent_comment_id,
                    PublicationCommentRecord.publication_id == publication_id,
                )
                .first()
            )
            if not parent:
                raise ValueError("Parent comment not found on this publication")

        comment = PublicationCommentRecord(
            publication_id=publication_id,
            user_id=user_id,
            content=content,
            parent_comment_id=parent_comment_id,
        )
        posts_db.add(comment)
        pub.total_comments_count += 1
        posts_db.commit()
        posts_db.refresh(comment)

        cache_service.incr_comment(str(publication_id), 1)
        cache_service.bump_trending(str(publication_id), weight=0.5)
        return comment

    def get_comment_thread(
        self, posts_db: Session, users_db: Session, publication_id: str
    ) -> List[Dict[str, Any]]:
        rows = (
            posts_db.query(PublicationCommentRecord)
            .filter(PublicationCommentRecord.publication_id == publication_id)
            .order_by(PublicationCommentRecord.created_timestamp.asc())
            .all()
        )

        publisher_ids = list({str(c.user_id) for c in rows})
        users = (
            users_db.query(PlatformUserRecord)
            .filter(PlatformUserRecord.user_id.in_(publisher_ids))
            .all()
        )
        user_map = {str(u.user_id): u for u in users}

        nodes: Dict[str, Dict[str, Any]] = {}
        for c in rows:
            publisher = user_map.get(str(c.user_id))
            nodes[str(c.comment_id)] = {
                "comment_id": c.comment_id,
                "publication_id": c.publication_id,
                "user_id": c.user_id,
                "publisher_handle": publisher.user_handle if publisher else "Unknown User",
                "publisher_avatar_url": publisher.avatar_storage_url if publisher else "",
                "parent_comment_id": c.parent_comment_id,
                "content": c.content,
                "is_edited": bool(c.is_edited),
                "created_timestamp": c.created_timestamp,
                "updated_timestamp": c.updated_timestamp,
                "replies": [],
            }

        roots: List[Dict[str, Any]] = []
        for node in nodes.values():
            parent_id = str(node["parent_comment_id"]) if node["parent_comment_id"] else None
            if parent_id and parent_id in nodes:
                nodes[parent_id]["replies"].append(node)
            else:
                roots.append(node)
        return roots

    # ----------------------------------------------------------------
    # Create publication (with approved categories) + bust search cache
    # ----------------------------------------------------------------
    def create_publication(
        self, posts_db: Session, users_db: Session, publisher_user_id: str,
        title: str, geojson: Dict[str, Any], metadata: Dict[str, Any],
        category_ids: List[int],
    ) -> Dict[str, Any]:
        pub = GeographicPublicationRecord(
            publisher_user_id=publisher_user_id,
            publication_title=title,
            spatial_geometry=text("ST_GeomFromGeoJSON(:gj)").bindparams(gj=json.dumps(geojson)),
            layer_attribute_metadata=metadata,
            share_slug=_make_share_slug(),
        )
        posts_db.add(pub)
        posts_db.flush()

        for cid in category_ids:
            cat = posts_db.query(CategoryRecord).filter(CategoryRecord.category_id == cid).first()
            if cat:
                posts_db.add(PublicationCategoryLink(publication_id=pub.publication_id, category_id=cid))
                cat.usage_count += 1

        # keep publisher's publication_count fresh
        publisher = (
            users_db.query(PlatformUserRecord)
            .filter(PlatformUserRecord.user_id == publisher_user_id)
            .first()
        )
        if publisher:
            publisher.publication_count += 1
            users_db.commit()

        posts_db.commit()
        posts_db.refresh(pub)

        cache_service.invalidate_search()  # new content should appear immediately

        rows = posts_db.execute(
            text(self._select_sql(where="WHERE publication_id = :pid", order="")),
            {"pid": str(pub.publication_id), "skip": 0, "limit": 1},
        ).fetchall()
        return self._hydrate(posts_db, users_db, rows)[0]


post_service = GeographicPublicationService()