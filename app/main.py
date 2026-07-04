import uuid
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis

from app.core.config import settings
from app.db.database import (
    users_engine, posts_engine, UsersSessionLocal, PostsSessionLocal,
    UsersBase, PostsBase,
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, UserLocationRecord,
    PostRecord, CategoryRecord, KeywordRecord, PostCategoryLink, PostKeywordLink,
)
from app.api.v1.endpoints import auth, posts, categories, follows, users, uploads, search
from app.services.auth_service import auth_service

# ── 1. Database initialisation ────────────────────────────────────────────────
UsersBase.metadata.create_all(bind=users_engine)
PostsBase.metadata.create_all(bind=posts_engine)


# ── 2. Lifespan — init Redis cache on startup ─────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf8",
        decode_responses=True,
    )
    FastAPICache.init(RedisBackend(redis), prefix="gisviz-cache")
    yield
    await redis.aclose()


# ── 3. Application setup ──────────────────────────────────────────────────────
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

# ── 4. Static files ───────────────────────────────────────────────────────────
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ── 5. Routes ─────────────────────────────────────────────────────────────────
app.include_router(auth.router,       prefix=f"{settings.API_V1_STR}/auth",       tags=["Authentication"])
app.include_router(users.router,      prefix=f"{settings.API_V1_STR}/users",      tags=["Users"])
app.include_router(uploads.router,    prefix=f"{settings.API_V1_STR}/uploads",    tags=["Uploads"])
app.include_router(posts.router,      prefix=f"{settings.API_V1_STR}/posts",      tags=["Posts"])
app.include_router(categories.router, prefix=f"{settings.API_V1_STR}/categories", tags=["Categories"])
app.include_router(follows.router,    prefix=f"{settings.API_V1_STR}/network",    tags=["Social Graph"])
app.include_router(search.router,     prefix=f"{settings.API_V1_STR}/search",     tags=["Search"])


# ── 6. Health check ───────────────────────────────────────────────────────────
@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api", "version": settings.VERSION}


# ── 7. Dev seed ───────────────────────────────────────────────────────────────
@app.get("/seed")
def seed_database():
    """
    Idempotent development seed.
    Safe to call multiple times — every insert is guarded by an existence
    check on ALL unique columns, preventing UniqueViolation errors.
    """
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:

        # ── Roles ─────────────────────────────────────────────────────────────
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

        # ── Admin user ────────────────────────────────────────────────────────
        # Guard on BOTH unique columns (user_handle and email_address) so a
        # partial previous seed cannot cause a UniqueViolation on re-run.
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

            # Location — only created alongside the user
            loc = UserLocationRecord(
                user_id=admin_user.user_id,
                place="Boulder",
                state="Colorado",
                country="United States",
                formatted_string="Boulder, Colorado, United States",
            )
            users_db.add(loc)
            users_db.commit()

        # ── Seed categories ───────────────────────────────────────────────────
        default_categories = [
            {"slug": "dem",              "label": "DEM"},
            {"slug": "satellite",        "label": "Satellite"},
            {"slug": "urban-planning",   "label": "Urban Planning"},
            {"slug": "climate",          "label": "Climate"},
            {"slug": "hydrology",        "label": "Hydrology"},
            {"slug": "land-cover",       "label": "Land Cover"},
            {"slug": "remote-sensing",   "label": "Remote Sensing"},
            {"slug": "transportation",   "label": "Transportation"},
        ]
        for c in default_categories:
            if not posts_db.query(CategoryRecord).filter(CategoryRecord.slug == c["slug"]).first():
                posts_db.add(CategoryRecord(slug=c["slug"], label=c["label"]))
        posts_db.commit()

        return {
            "status": "Success",
            "message": "Database seeded successfully. Safe to call multiple times.",
        }

    except Exception as e:
        users_db.rollback()
        posts_db.rollback()
        return {"status": "Error", "details": str(e)}
    finally:
        users_db.close()
        posts_db.close()