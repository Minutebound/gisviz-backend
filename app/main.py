import uuid
from datetime import datetime
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import (
    users_engine, posts_engine,
    UsersSessionLocal, PostsSessionLocal,
    UsersBase, PostsBase,
    get_users_db, get_posts_db,
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, FollowEventRecord, FollowCurrentRecord,
    GeographicPublicationRecord, PublicationLikeRecord, PublicationCommentRecord,
    CategoryRecord, PublicationCategoryLink, PendingTagRecord,
)
from app.api.v1.endpoints import auth, posts, categories, follows
from app.services.auth_service import auth_service
from app.services.post_service import post_service, _make_share_slug


# ---------------------------------------------------------
# 1. Database Initialization
# ---------------------------------------------------------
# Ensure PostGIS exists on the posts DB before creating geometry columns
with posts_engine.connect() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    conn.commit()

UsersBase.metadata.create_all(bind=users_engine)
PostsBase.metadata.create_all(bind=posts_engine)


# ---------------------------------------------------------
# 2. Application Setup
# ---------------------------------------------------------
app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# 3. Route Registration
# ---------------------------------------------------------
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(posts.router, prefix=f"{settings.API_V1_STR}/publications", tags=["Publications"])
app.include_router(categories.router, prefix=f"{settings.API_V1_STR}/categories", tags=["Categories"])
app.include_router(follows.router, prefix=f"{settings.API_V1_STR}/users", tags=["Social Graph"])


@app.get(f"{settings.API_V1_STR}/publications", tags=["Publications"])
@app.get(f"{settings.API_V1_STR}/publications/", tags=["Publications"])
def get_publication_feed(
    skip: int = 0,
    limit: int = 50,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    return post_service.retrieve_global_stream(
        posts_db=posts_db, users_db=users_db, skip=skip, limit=limit
    )


@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api", "version": settings.VERSION}


# ---------------------------------------------------------
# 4. Seed Endpoint (development only — remove/guard in production)
# ---------------------------------------------------------
@app.get("/seed")
def seed_database():
    """Development seed — populates every table with at least 2 records.

    Tables populated:
      users-db:
        - roles                (4)
        - platform_users       (5)
        - follow_events        (8: includes follow/unfollow churn)
        - follows_current      (6: net active follows after churn)
      posts-db:
        - categories              (21)
        - geographic_publications (8)
        - publication_categories  (~20 join rows)
        - publication_likes       (12)
        - publication_comments    (10, includes threaded replies)
        - pending_tags            (3: 1 pending, 1 approved, 1 rejected)
    """
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:
        # ============================================================
        # 1. ROLES
        # ============================================================
        role_defs = [
            {"name": "admin",     "permissions": {"manage_tags": True, "moderate": True, "publish": True, "admin": True}},
            {"name": "moderator", "permissions": {"manage_tags": True, "moderate": True, "publish": True}},
            {"name": "publisher", "permissions": {"publish": True}},
            {"name": "viewer",    "permissions": {}},
        ]
        role_map = {}
        for rd in role_defs:
            role = users_db.query(RoleRecord).filter(RoleRecord.name == rd["name"]).first()
            if not role:
                role = RoleRecord(name=rd["name"], permissions=rd["permissions"])
                users_db.add(role)
                users_db.commit()
                users_db.refresh(role)
            role_map[rd["name"]] = role

        # ============================================================
        # 2. CATEGORIES
        # ============================================================
        category_defs = [
            "Raster", "DEM", "Terrain", "Satellite", "NDVI", "Conservation",
            "Vector", "Network Analysis", "Transit", "Bathymetry", "Sonar",
            "Oceanography", "Urban Planning", "Cadastral", "Airspace", "Lines",
            "LiDAR", "3D", "Point Cloud", "Thermal Imagery", "Disaster Response",
        ]
        cat_map = {}
        for label in category_defs:
            slug = label.lower().replace(" ", "-")
            cat = posts_db.query(CategoryRecord).filter(CategoryRecord.slug == slug).first()
            if not cat:
                cat = CategoryRecord(slug=slug, label=label)
                posts_db.add(cat)
                posts_db.commit()
                posts_db.refresh(cat)
            cat_map[label] = cat

        # ============================================================
        # 3. USERS
        # ============================================================
        mock_users = [
            {"handle": "david_c",      "email": "david@gisviz.com",   "role": "publisher", "avatar": "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=150"},
            {"handle": "eco_mapper",   "email": "emily@gisviz.com",   "role": "publisher", "avatar": "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=150"},
            {"handle": "transit_data", "email": "transit@gisviz.com", "role": "publisher", "avatar": "https://images.unsplash.com/photo-1599566150163-29194dcaad36?w=150"},
            {"handle": "lidar_ninja",  "email": "sarah@gisviz.com",   "role": "moderator", "avatar": "https://images.unsplash.com/photo-1438761681033-6461ffad8d80?w=150"},
            {"handle": "geo_analyst",  "email": "marcus@gisviz.com",  "role": "admin",     "avatar": "https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=150"},
        ]
        created_users = []
        for u in mock_users:
            user = users_db.query(PlatformUserRecord).filter(
                PlatformUserRecord.user_handle == u["handle"]
            ).first()
            if not user:
                user = PlatformUserRecord(
                    user_id=uuid.uuid4(),
                    user_handle=u["handle"],
                    email_address=u["email"],
                    hashed_security_password=auth_service.get_password_hash("Password123!"),
                    avatar_storage_url=u["avatar"],
                    role_id=role_map[u["role"]].role_id,
                )
                users_db.add(user)
                users_db.commit()
                users_db.refresh(user)
            created_users.append(user)

        # ============================================================
        # 4. FOLLOW EVENTS + FOLLOWS_CURRENT (with churn for realism)
        # ============================================================
        users_db.query(FollowCurrentRecord).delete()
        users_db.query(FollowEventRecord).delete()
        for user in created_users:
            user.follower_count = 0
            user.following_count = 0
        users_db.commit()

        follow_actions = [
            (1, 0, "follow"),     # eco_mapper -> david_c
            (2, 0, "follow"),     # transit_data -> david_c
            (3, 0, "follow"),     # lidar_ninja -> david_c
            (4, 0, "follow"),     # geo_analyst -> david_c
            (0, 1, "follow"),     # david_c -> eco_mapper
            (3, 1, "follow"),     # lidar_ninja -> eco_mapper
            (3, 1, "unfollow"),   # ...then unfollows (churn audit trail)
            (0, 2, "follow"),     # david_c -> transit_data
        ]

        for actor_i, target_i, action in follow_actions:
            actor = created_users[actor_i]
            target = created_users[target_i]

            users_db.add(FollowEventRecord(
                actor_user_id=actor.user_id,
                target_user_id=target.user_id,
                action=action,
            ))

            current = users_db.query(FollowCurrentRecord).filter(
                FollowCurrentRecord.actor_user_id == actor.user_id,
                FollowCurrentRecord.target_user_id == target.user_id,
            ).first()

            if action == "follow" and not current:
                users_db.add(FollowCurrentRecord(
                    actor_user_id=actor.user_id,
                    target_user_id=target.user_id,
                ))
                actor.following_count += 1
                target.follower_count += 1
            elif action == "unfollow" and current:
                users_db.delete(current)
                actor.following_count = max(0, actor.following_count - 1)
                target.follower_count = max(0, target.follower_count - 1)

        users_db.commit()

        # ============================================================
        # 5. PUBLICATIONS (wipe-and-reseed for clean state)
        # ============================================================
        posts_db.query(PublicationCommentRecord).delete()
        posts_db.query(PublicationLikeRecord).delete()
        posts_db.query(PublicationCategoryLink).delete()
        posts_db.query(GeographicPublicationRecord).delete()
        for user in created_users:
            user.publication_count = 0
        users_db.commit()
        posts_db.commit()

        mock_posts = [
            {"publisher": 0, "title": "Global Elevation Modeling: Rocky Mountains",  "cats": ["Raster", "DEM", "Terrain"],             "likes": 342,  "comments": 28,  "lon": -105.6836, "lat": 40.3428},
            {"publisher": 1, "title": "Amazon Rainforest Canopy Loss (2020-2025)",   "cats": ["Satellite", "NDVI", "Conservation"],    "likes": 892,  "comments": 145, "lon": -60.0250,  "lat": -3.1190},
            {"publisher": 2, "title": "Tokyo High-Speed Rail Isochrone Matrices",    "cats": ["Vector", "Network Analysis", "Transit"], "likes": 512,  "comments": 63,  "lon": 139.7671,  "lat": 35.6812},
            {"publisher": 3, "title": "Bathymetric Scan of the Mariana Trench",      "cats": ["Bathymetry", "Sonar", "Oceanography"],  "likes": 1024, "comments": 89,  "lon": 142.2000,  "lat": 11.3500},
            {"publisher": 4, "title": "Manhattan Commercial Zoning Parcels",         "cats": ["Urban Planning", "Cadastral"],          "likes": 256,  "comments": 12,  "lon": -73.9712,  "lat": 40.7831},
            {"publisher": 0, "title": "European Flight Corridors & Density",         "cats": ["Airspace", "Lines"],                    "likes": 415,  "comments": 34,  "lon": 4.4699,    "lat": 50.5039},
            {"publisher": 3, "title": "San Francisco LiDAR Point Cloud",             "cats": ["LiDAR", "3D", "Point Cloud"],           "likes": 780,  "comments": 56,  "lon": -122.4194, "lat": 37.7749},
            {"publisher": 1, "title": "Australian Wildfire Progression Spread",      "cats": ["Thermal Imagery", "Disaster Response"], "likes": 630,  "comments": 92,  "lon": 149.0124,  "lat": -35.4735},
        ]

        created_pub_ids = []
        for p in mock_posts:
            pub_id = uuid.uuid4()
            posts_db.execute(
                text("""
                    INSERT INTO geographic_publications (
                        publication_id, publisher_user_id, publication_title,
                        spatial_geometry, layer_attribute_metadata,
                        share_slug, total_likes_count, total_comments_count
                    ) VALUES (
                        :pub_id, :publisher_id, :title,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                        :metadata,
                        :slug, :likes, :comments
                    )
                """),
                {
                    "pub_id":       str(pub_id),
                    "publisher_id": str(created_users[p["publisher"]].user_id),
                    "title":        p["title"],
                    "lon":          p["lon"],
                    "lat":          p["lat"],
                    "metadata":     '{"zoom": 12, "projection": "EPSG:4326"}',
                    "slug":         _make_share_slug(),
                    "likes":        p["likes"],
                    "comments":     p["comments"],
                },
            )
            created_pub_ids.append(pub_id)

            # ---- Category links ----
            for label in p["cats"]:
                cat = cat_map[label]
                posts_db.execute(
                    text("""
                        INSERT INTO publication_categories (publication_id, category_id)
                        VALUES (:pub_id, :cat_id)
                    """),
                    {"pub_id": str(pub_id), "cat_id": cat.category_id},
                )
                cat.usage_count += 1

            created_users[p["publisher"]].publication_count += 1

        posts_db.commit()
        users_db.commit()

        # ============================================================
        # 6. PUBLICATION LIKES (12 rows across 4 publications)
        # ============================================================
        like_pairs = [
            (0, 1), (0, 2), (0, 3),                # 3 users like Rocky Mountains
            (1, 0), (1, 2), (1, 3), (1, 4),        # 4 users like Amazon
            (2, 0), (2, 3),                        # 2 users like Tokyo
            (3, 0), (3, 1), (3, 4),                # 3 users like Mariana
        ]
        for pub_i, user_i in like_pairs:
            posts_db.add(PublicationLikeRecord(
                publication_id=created_pub_ids[pub_i],
                user_id=created_users[user_i].user_id,
            ))
        posts_db.commit()

        # ============================================================
        # 7. PUBLICATION COMMENTS (with threaded replies)
        # ============================================================
        top_comments = [
            {"pub": 0, "user": 2, "content": "Incredible resolution on the western face."},
            {"pub": 0, "user": 3, "content": "What was the LiDAR pulse density on this?"},
            {"pub": 1, "user": 0, "content": "The temporal baseline here really shows the loss trends."},
            {"pub": 1, "user": 4, "content": "Could you share the methodology paper?"},
            {"pub": 2, "user": 3, "content": "Isochrone bands are spot on. JR East schedule?"},
            {"pub": 3, "user": 0, "content": "Sonar coverage looks dense — multibeam EM124?"},
            {"pub": 4, "user": 1, "content": "Nice parcel-level fidelity here."},
            {"pub": 7, "user": 2, "content": "Thermal banding is striking. Source: MODIS or Sentinel-3?"},
        ]
        created_top_comment_ids = []
        for c in top_comments:
            comment = PublicationCommentRecord(
                publication_id=created_pub_ids[c["pub"]],
                user_id=created_users[c["user"]].user_id,
                content=c["content"],
            )
            posts_db.add(comment)
            posts_db.flush()  # need comment_id for replies
            created_top_comment_ids.append(comment.comment_id)

        # Threaded replies — answer the first two top-level comments
        replies = [
            {"parent_index": 0, "pub": 0, "user": 0, "content": "Thanks — flown at 12 ppm with overlap stitching."},
            {"parent_index": 1, "pub": 0, "user": 0, "content": "Around 8 returns/m² in the high-density passes."},
        ]
        for r in replies:
            posts_db.add(PublicationCommentRecord(
                publication_id=created_pub_ids[r["pub"]],
                user_id=created_users[r["user"]].user_id,
                content=r["content"],
                parent_comment_id=created_top_comment_ids[r["parent_index"]],
            ))
        posts_db.commit()

        # ============================================================
        # 8. PENDING TAGS (one pending, one approved, one rejected)
        # ============================================================
        posts_db.query(PendingTagRecord).delete()
        posts_db.commit()

        pending_defs = [
            {"label": "Hydrology",        "status": "pending",  "reviewed": None,              "user": 1},
            {"label": "Glacial Geology",  "status": "approved", "reviewed": datetime.utcnow(), "user": 3},
            {"label": "spammy tag!!!",    "status": "rejected", "reviewed": datetime.utcnow(), "user": 2},
        ]
        for pt in pending_defs:
            slug = pt["label"].strip().lower().replace(" ", "-").replace("!", "")[:60]
            posts_db.add(PendingTagRecord(
                label=pt["label"],
                normalized_slug=slug,
                suggested_by_user_id=created_users[pt["user"]].user_id,
                status=pt["status"],
                reviewed_timestamp=pt["reviewed"],
            ))
        posts_db.commit()

        # ============================================================
        # SUMMARY
        # ============================================================
        return {
            "status": "Success",
            "seeded": {
                "users_db": {
                    "roles":           len(role_defs),
                    "platform_users":  len(mock_users),
                    "follow_events":   len(follow_actions),
                    "follows_current": users_db.query(FollowCurrentRecord).count(),
                },
                "posts_db": {
                    "categories":              len(category_defs),
                    "geographic_publications": len(mock_posts),
                    "publication_categories":  sum(len(p["cats"]) for p in mock_posts),
                    "publication_likes":       len(like_pairs),
                    "publication_comments":    len(top_comments) + len(replies),
                    "pending_tags":            len(pending_defs),
                },
            },
            "tip": "Remove or role-gate this endpoint before production.",
        }

    except Exception as e:
        users_db.rollback()
        posts_db.rollback()
        return {"status": "Error", "details": str(e)}
    finally:
        users_db.close()
        posts_db.close()