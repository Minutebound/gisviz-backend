import uuid
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
    GeographicPublicationRecord, CategoryRecord, PublicationCategoryLink,
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
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:
        # ---- Roles ----
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

        # ---- Categories ----
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

        # ---- Users ----
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

        # ---- Follow events ----
        follow_pairs = [
            (1, 0), (2, 0), (3, 0), (4, 0),
            (0, 1), (3, 1),
            (0, 2),
        ]
        for actor_i, target_i in follow_pairs:
            actor, target = created_users[actor_i], created_users[target_i]
            exists = users_db.query(FollowCurrentRecord).filter(
                FollowCurrentRecord.actor_user_id == actor.user_id,
                FollowCurrentRecord.target_user_id == target.user_id,
            ).first()
            if not exists:
                users_db.add(FollowEventRecord(
                    actor_user_id=actor.user_id,
                    target_user_id=target.user_id,
                    action="follow",
                ))
                users_db.add(FollowCurrentRecord(
                    actor_user_id=actor.user_id,
                    target_user_id=target.user_id,
                ))
                actor.following_count += 1
                target.follower_count += 1
        users_db.commit()

        # ---- Publications — wipe and re-seed cleanly ----
        posts_db.query(PublicationCategoryLink).delete()
        posts_db.query(GeographicPublicationRecord).delete()
        posts_db.commit()

        mock_posts = [
            {"author": 0, "title": "Global Elevation Modeling: Rocky Mountains", "cats": ["Raster", "DEM", "Terrain"],              "likes": 342,  "comments": 28,  "lon": -105.6836, "lat": 40.3428},
            {"author": 1, "title": "Amazon Rainforest Canopy Loss (2020-2025)",  "cats": ["Satellite", "NDVI", "Conservation"],     "likes": 892,  "comments": 145, "lon": -60.0250,  "lat": -3.1190},
            {"author": 2, "title": "Tokyo High-Speed Rail Isochrone Matrices",   "cats": ["Vector", "Network Analysis", "Transit"],  "likes": 512,  "comments": 63,  "lon": 139.7671,  "lat": 35.6812},
            {"author": 3, "title": "Bathymetric Scan of the Mariana Trench",     "cats": ["Bathymetry", "Sonar", "Oceanography"],   "likes": 1024, "comments": 89,  "lon": 142.2000,  "lat": 11.3500},
            {"author": 4, "title": "Manhattan Commercial Zoning Parcels",        "cats": ["Urban Planning", "Cadastral"],           "likes": 256,  "comments": 12,  "lon": -73.9712,  "lat": 40.7831},
            {"author": 0, "title": "European Flight Corridors & Density",        "cats": ["Airspace", "Lines"],                     "likes": 415,  "comments": 34,  "lon": 4.4699,    "lat": 50.5039},
            {"author": 3, "title": "San Francisco LiDAR Point Cloud",            "cats": ["LiDAR", "3D", "Point Cloud"],            "likes": 780,  "comments": 56,  "lon": -122.4194, "lat": 37.7749},
            {"author": 1, "title": "Australian Wildfire Progression Spread",     "cats": ["Thermal Imagery", "Disaster Response"],  "likes": 630,  "comments": 92,  "lon": 149.0124,  "lat": -35.4735},
        ]

        for p in mock_posts:
            # Use ST_GeomFromText via raw SQL insert so GeoAlchemy2
            # receives a proper geometry — not a raw WKT string.
            pub_id = uuid.uuid4()
            posts_db.execute(
                text("""
                    INSERT INTO geographic_publications (
                        publication_id, author_user_id, publication_title,
                        spatial_geometry, layer_attribute_metadata,
                        share_slug, total_likes_count, total_comments_count
                    ) VALUES (
                        :pub_id, :author_id, :title,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                        :metadata,
                        :slug, :likes, :comments
                    )
                """),
                {
                    "pub_id":    str(pub_id),
                    "author_id": str(created_users[p["author"]].user_id),
                    "title":     p["title"],
                    "lon":       p["lon"],
                    "lat":       p["lat"],
                    "metadata":  '{"zoom": 12, "projection": "EPSG:4326"}',
                    "slug":      _make_share_slug(),
                    "likes":     p["likes"],
                    "comments":  p["comments"],
                },
            )
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

            created_users[p["author"]].publication_count += 1

        posts_db.commit()
        users_db.commit()

        return {
            "status": "Success",
            "seeded": {
                "roles":        len(role_defs),
                "categories":   len(category_defs),
                "users":        len(mock_users),
                "publications": len(mock_posts),
            },
            "tip": "Hit this endpoint only once. Remove or guard it before production.",
        }

    except Exception as e:
        users_db.rollback()
        posts_db.rollback()
        return {"status": "Error", "details": str(e)}
    finally:
        users_db.close()
        posts_db.close()