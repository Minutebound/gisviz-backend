"""
app/main.py
─────────────────────────────
Change from previous version:
  FastAPICache is now properly initialised in the lifespan so the
  @cache decorator on any endpoint that still uses it actually works.
  (Previously the lifespan just yielded with no init, so @cache was
  a silent no-op and every decorated request hit Postgres directly.)

  The search endpoint no longer uses @cache — it uses CacheService
  directly so we can invalidate on mutations. But FastAPICache is
  still init'd here because other future endpoints or libraries may
  need it, and the cost is zero.
"""

import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from app.core.config import settings
from app.db.database import (
    UsersBase, PostsBase, AnalyticsBase, AdminBase,
    users_engine, posts_engine, analytics_engine, admin_engine,UsersSessionLocal, 
    PostsSessionLocal, 
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, UserLocationRecord, CategoryRecord,
)

from app.api.v1.endpoints import auth, posts, categories, follows, users, uploads, search
from app.api.v1.endpoints import admin as admin_endpoints
from app.services.auth_service import auth_service
import uuid

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Initialise FastAPICache with the Redis backend ────────────────
    # Uses an async Redis client (required by fastapi-cache2 ≥ 0.2).
    # The sync CacheService in cache_service.py uses its own separate
    # sync client — both point to the same Redis instance, which is fine.
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    FastAPICache.init(
        RedisBackend(redis_client),
        prefix="gisviz-cache",   # all @cache keys are namespaced under this prefix
    )
    print("[FastAPICache] Redis backend initialised")

    yield  # ← app runs here

    # Graceful shutdown
    await redis_client.aclose()
    print("[FastAPICache] Redis connection closed")


# ── Create tables for all four databases on startup ──────────────────
UsersBase.metadata.create_all(bind=users_engine)
PostsBase.metadata.create_all(bind=posts_engine)
AnalyticsBase.metadata.create_all(bind=analytics_engine)
AdminBase.metadata.create_all(bind=admin_engine)

# ── Application setup ────────────────────────────────────────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ── Routes ───────────────────────────────────────────────────────────
app.include_router(auth.router,            prefix=f"{settings.API_V1_STR}/auth",       tags=["Authentication"])
app.include_router(users.router,           prefix=f"{settings.API_V1_STR}/users",      tags=["Users"])
app.include_router(posts.router,           prefix=f"{settings.API_V1_STR}/posts",      tags=["Posts"])
app.include_router(categories.router,      prefix=f"{settings.API_V1_STR}/categories", tags=["Categories"])
app.include_router(follows.router,         prefix=f"{settings.API_V1_STR}/network",    tags=["Social Graph"])
app.include_router(uploads.router,         prefix=f"{settings.API_V1_STR}/uploads",    tags=["Uploads"])
app.include_router(search.router,          prefix=f"{settings.API_V1_STR}/search",     tags=["Search"])
app.include_router(admin_endpoints.router, prefix=f"{settings.API_V1_STR}/admin",      tags=["Admin"])

@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api", "version": settings.VERSION}


@app.get("/seed")
def seed_database():
    """Development seed — adapted for the new static-image Post schema."""
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:
        # ROLES
        role_defs = [
            {"name": "admin",     "permissions": {"manage_tags": True, "moderate": True, "publish": True, "admin": True}},
            {"name": "editor",    "permissions": {"manage_tags": True, "moderate": True, "publish": True}},
            {"name": "publisher", "permissions": {"publish": True}},
            {"name": "viewer",    "permissions": {}},
            {"name": "support",   "permissions": {"view_reports": True}},
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

        # ADMIN USER — guard on both unique columns to prevent UniqueViolation on re-run
        ADMIN_HANDLE = "system_admin"
        ADMIN_EMAIL  = "admin@gisviz.com"

        admin_user = (
            users_db.query(PlatformUserRecord)
            .filter(
                (PlatformUserRecord.user_handle   == ADMIN_HANDLE) |
                (PlatformUserRecord.email_address == ADMIN_EMAIL)
            )
            .first()
        )

        if not admin_user:
            admin_user = PlatformUserRecord(
                user_id=uuid.uuid4(),
                user_handle=ADMIN_HANDLE,
                email_address=ADMIN_EMAIL,
                hashed_security_password=auth_service.get_password_hash("Password123!"),
                title="Lead Architect",
                role_id=role_map["admin"].role_id,
                is_verified=1,
                is_active=1,
            )
            users_db.add(admin_user)
            users_db.commit()
            users_db.refresh(admin_user)

            loc = UserLocationRecord(
                user_id=admin_user.user_id,
                place="Boulder", state="Colorado", country="United States",
                formatted_string="Boulder, Colorado, United States",
            )
            users_db.add(loc)
            users_db.commit()

        # CATEGORIES
        default_categories = [
            {"slug": "dem",            "label": "DEM"},
            {"slug": "satellite",      "label": "Satellite"},
            {"slug": "urban-planning", "label": "Urban Planning"},
            {"slug": "climate",        "label": "Climate"},
            {"slug": "hydrology",      "label": "Hydrology"},
            {"slug": "land-cover",     "label": "Land Cover"},
            {"slug": "remote-sensing", "label": "Remote Sensing"},
            {"slug": "transportation", "label": "Transportation"},
        ]
        for c in default_categories:
            if not posts_db.query(CategoryRecord).filter(CategoryRecord.slug == c["slug"]).first():
                posts_db.add(CategoryRecord(slug=c["slug"], label=c["label"]))
        posts_db.commit()

        return {"status": "Success", "message": "Database seeded successfully."}

    except Exception as e:
        users_db.rollback()
        posts_db.rollback()
        return {"status": "Error", "details": str(e)}
    finally:
        users_db.close()
        posts_db.close()