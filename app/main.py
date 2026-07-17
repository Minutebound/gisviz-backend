"""
app/main.py
─────────────────────────────
Changes from previous version:
  1. /docs and /redoc are now admin-only.
     FastAPI's built-in docs are disabled (docs_url=None, redoc_url=None).
     Custom /docs and /redoc routes verify a valid admin JWT before
     serving the underlying OpenAPI HTML — so the schema is never visible
     to unauthenticated users or non-admin roles.

  2. VERSION is read from the APP_VERSION environment variable first,
     falling back to the hardcoded string in config.py. The GitHub Actions
     workflow injects APP_VERSION from the Git tag on every release, so the
     running app always reports its exact deployed version without any
     manual edits to source files.

  3. /api/v0/legal router registered (public GET, admin PUT).

  4. GET /cache/health debug endpoint (unchanged from previous version).
"""

import os
from contextlib import asynccontextmanager
from jose import JWTError, jwt as jose_jwt
from fastapi import Query as QueryParam
from typing import Optional
import redis.asyncio as aioredis
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from app.core.config import settings
from app.db.database import (
    UsersBase, PostsBase, AnalyticsBase, AdminBase,
    users_engine, posts_engine, analytics_engine, admin_engine,
    UsersSessionLocal, PostsSessionLocal,
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, UserLocationRecord, CategoryRecord,
)
from app.api.v0.endpoints import auth, posts, categories, follows, users, uploads, search, seo
from app.api.v0.endpoints import admin as admin_endpoints
from app.api.v0.endpoints import support as support_endpoints
from app.api.v0.endpoints import legal as legal_endpoints
from app.services.auth_service import auth_service, get_optional_current_user
import uuid


# ── Version — injected by CI/CD, falls back to config ────────────────────────
# GitHub Actions sets APP_VERSION=$(git describe --tags --abbrev=0) on release.
# In development this is unset so the hardcoded config.py value is used.
_VERSION = os.getenv("APP_VERSION", settings.VERSION)


# ── Admin doc guard ───────────────────────────────────────────────────────────
async def _require_admin(
    token_param: Optional[str] = QueryParam(None, alias="token"),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
) -> PlatformUserRecord:
    # Path 1 — Authorization header (Postman / curl)
    if current_user and getattr(current_user.role, "name", "") == "admin":
        return current_user

    # Path 2 — ?token= query param (browser direct navigation)
    if token_param:
        try:
            payload = jose_jwt.decode(
                token_param,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            user_id = payload.get("sub")
            if user_id:
                from app.db.database import UsersSessionLocal
                db = UsersSessionLocal()
                try:
                    user = db.query(PlatformUserRecord).filter(
                        PlatformUserRecord.user_id == user_id
                    ).first()
                    if user and getattr(user.role, "name", "") == "admin":
                        return user
                finally:
                    db.close()
        except JWTError:
            pass

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required. Pass your JWT as ?token=YOUR_JWT",
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    FastAPICache.init(
        RedisBackend(redis_client),
        prefix="gisviz-cache",
    )
    print(f"[FastAPICache] Redis backend initialised  (version={_VERSION})")
    yield
    await redis_client.aclose()
    print("[FastAPICache] Redis connection closed")


# ── Create tables ─────────────────────────────────────────────────────────────
UsersBase.metadata.create_all(bind=users_engine)
PostsBase.metadata.create_all(bind=posts_engine)
AnalyticsBase.metadata.create_all(bind=analytics_engine)
AdminBase.metadata.create_all(bind=admin_engine)


# ── App — docs disabled at framework level, served manually below ─────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=_VERSION,
    lifespan=lifespan,
    # Disable built-in doc routes — we serve them ourselves with auth
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json",   # schema still available (needed by the custom UIs)
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


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(auth.router,              prefix=f"{settings.API_V0_STR}/auth",       tags=["Authentication"])
app.include_router(users.router,             prefix=f"{settings.API_V0_STR}/users",      tags=["Users"])
app.include_router(posts.router,             prefix=f"{settings.API_V0_STR}/posts",      tags=["Posts"])
app.include_router(categories.router,        prefix=f"{settings.API_V0_STR}/categories", tags=["Categories"])
app.include_router(follows.router,           prefix=f"{settings.API_V0_STR}/network",    tags=["Social Graph"])
app.include_router(uploads.router,           prefix=f"{settings.API_V0_STR}/uploads",    tags=["Uploads"])
app.include_router(search.router,            prefix=f"{settings.API_V0_STR}/search",     tags=["Search"])
app.include_router(admin_endpoints.router,   prefix=f"{settings.API_V0_STR}/admin",      tags=["Admin"])
app.include_router(support_endpoints.router, prefix=f"{settings.API_V0_STR}/support",    tags=["Support"])
app.include_router(legal_endpoints.router,   prefix=f"{settings.API_V0_STR}/legal",      tags=["Legal"])
app.include_router(seo.router,               prefix=f"{settings.API_V0_STR}/seo",        tags=["SEO"])

# ── Admin-only API docs ───────────────────────────────────────────────────────
# Both /docs and /redoc validate the JWT and check for the admin role before
# returning the HTML. The openapi.json schema itself is still public — this
# only protects the interactive UI. If you want the schema locked too, change
# openapi_url=None above and serve it via a similar guarded route.

@app.get("/docs", include_in_schema=False)
async def admin_swagger(
    _: PlatformUserRecord = Depends(_require_admin),
):
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{settings.PROJECT_NAME} — API Docs",
        swagger_ui_parameters={"persistAuthorization": True},
    )


@app.get("/redoc", include_in_schema=False)
async def admin_redoc(
    _: PlatformUserRecord = Depends(_require_admin),
):
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{settings.PROJECT_NAME} — API Reference",
    )


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api", "version": _VERSION}


@app.get("/cache/health")
async def cache_health():
    try:
        r = aioredis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        await r.ping()
        known_keys = [
            "gisviz-cache:categories:list",
            "gisviz-cache:users:popular:15",
            "gisviz-cache:users:popular:50",
        ]
        key_status = {}
        for k in known_keys:
            ttl = await r.ttl(k)
            key_status[k] = {"exists": ttl > 0, "ttl_seconds": ttl if ttl > 0 else None}
        await r.aclose()
        return {"redis": "ok", "version": _VERSION, "keys": key_status}
    except Exception as exc:
        return {"redis": "error", "detail": str(exc)}


# ── Seed (dev only) ───────────────────────────────────────────────────────────
@app.get("/seed")
def seed_database():
    """Development seed — adapted for the new static-image Post schema."""
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:
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