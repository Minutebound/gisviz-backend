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
from fastapi import FastAPI, Depends, Header, HTTPException, status
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from app.core.config import settings
from app.db.database import (
    UsersBase, PostsBase, AnalyticsBase, AdminBase, DatasetsBase, MiscBase,
    users_engine, posts_engine, analytics_engine, admin_engine, datasets_engine, misc_engine,
    UsersSessionLocal, PostsSessionLocal,
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, UserLocationRecord, CategoryRecord,
)
from app.api.v0.endpoints import auth, posts, categories, follows, users, uploads, search, seo
from app.api.v0.endpoints import visuals as visuals_endpoints
from app.api.v0.endpoints import datasets as datasets_endpoints
from app.api.v0.endpoints import regions as regions_endpoints
from app.api.v0.endpoints import admin as admin_endpoints
from app.api.v0.endpoints import support as support_endpoints
from app.api.v0.endpoints import legal as legal_endpoints
from app.api.v0.endpoints import access as access_endpoints
from app.api.v0.endpoints import orgs as orgs_endpoints
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

    # Path 2 — ?token= query param (browser direct navigation); same session checks as the header path
    if token_param:
        from app.db.database import UsersSessionLocal
        from app.services.auth_service import _user_from_token
        db = UsersSessionLocal()
        try:
            user, _ = _user_from_token(token_param, db)
            if user and user.is_active and getattr(user.role, "name", "") == "admin":
                return user
        finally:
            db.close()

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
    try:                                # datasets DB vs gisviz.duckdb (missing tables -> inactive)
        from app.services import dataset_sync
        dataset_sync.startup_check()
    except Exception as e:              # never block start-up on this check
        print(f"[dataset_sync] start-up check skipped: {type(e).__name__}: {e}", flush=True)
    try:                                # analytics DB: daily stats captured every hour (SNAPSHOT_EVERY_SECONDS)
        from app.analytics.snapshot import start_scheduler
        start_scheduler()
    except Exception as e:
        print(f"[analytics] snapshot scheduler not started: {type(e).__name__}: {e}", flush=True)
    yield
    await redis_client.aclose()
    print("[FastAPICache] Redis connection closed")


# ── Create tables ─────────────────────────────────────────────────────────────
def _create_tables() -> None:
    """create_all per database under an advisory lock, so several uvicorn workers starting on a
    fresh database do not race each other (duplicate pg_type errors)."""
    from sqlalchemy import text
    for base, engine in ((UsersBase, users_engine), (PostsBase, posts_engine), (AnalyticsBase, analytics_engine),
                         (AdminBase, admin_engine), (DatasetsBase, datasets_engine),
                         (MiscBase, misc_engine)):
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(424241)"))
            base.metadata.create_all(bind=conn)


_create_tables()


def _upgrade_schemas() -> None:
    """create_all() never adds columns to tables that already exist, so columns added later
    are ensured here (idempotent, safe with several workers starting together)."""
    from sqlalchemy import text
    posts_stmts = [
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS dataset_id VARCHAR(64)",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS visual_spec JSONB",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS theme_color VARCHAR(7)",
        "CREATE INDEX IF NOT EXISTS ix_posts_dataset_id ON posts (dataset_id)",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS views_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS region VARCHAR(60)",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS backdrop JSONB",
        "CREATE INDEX IF NOT EXISTS ix_posts_region ON posts (region)",
        "CREATE INDEX IF NOT EXISTS ix_posts_views_count ON posts (views_count)",
        # private posts + live posts (stream datasets); every existing post stays public / standard
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'public'",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS post_type VARCHAR(10) NOT NULL DEFAULT 'standard'",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS deactivated_by_dataset INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_posts_visibility ON posts (visibility)",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS theme_color VARCHAR(7)",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS banner JSONB",
        # the catalog lives in its own database, so any old foreign key from posts.dataset_id to a
        # datasets table in the posts database is dropped, whatever it was named
        # (fk_posts_dataset, posts_dataset_id_fkey, ...); the link is checked in code instead
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conrelid = 'posts'::regclass AND c.contype = 'f' AND a.attname = 'dataset_id'
          LOOP
            EXECUTE format('ALTER TABLE posts DROP CONSTRAINT %I', r.conname);
          END LOOP;
        END $$
        """,
    ]
    users_stmts = [
        # sessions: no expiry, ended by logout (revoked_tokens) or password change (token_version)
        "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        # individual / organisation accounts
        "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS account_type VARCHAR(16) NOT NULL DEFAULT 'individual'",
    ]
    dataset_stmts = [
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS columns JSONB",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS data_uploaded_at TIMESTAMPTZ",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS region VARCHAR(60)",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS show_data INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_datasets_region ON datasets (region)",
        # private datasets, stream (live) pipelines, licensing, GDPR; every existing dataset stays public / batch
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'public'",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS owner_user_id UUID",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS pipeline VARCHAR(10) NOT NULL DEFAULT 'batch'",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS refresh_seconds INTEGER NOT NULL DEFAULT 60",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS retention_rows INTEGER",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS ingest_key_hash VARCHAR(64)",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS last_ingested_at TIMESTAMPTZ",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS license_code VARCHAR(40)",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS attribution TEXT",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS copyright_holder VARCHAR(255)",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS contains_personal_data INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_datasets_visibility ON datasets (visibility)",
        "CREATE INDEX IF NOT EXISTS ix_datasets_pipeline ON datasets (pipeline)",
        "CREATE INDEX IF NOT EXISTS ix_datasets_owner_user_id ON datasets (owner_user_id)",
        # two states only: active / inactive (older draft/hidden/retired rows become inactive)
        "ALTER TABLE datasets ALTER COLUMN status SET DEFAULT 'inactive'",
        "UPDATE datasets SET status = 'inactive' WHERE status NOT IN ('active', 'inactive') "
        "OR (status = 'active' AND columns IS NULL)",
        # dataset codes ds_00001, ds_00002, ... continue after the highest existing one
        "CREATE SEQUENCE IF NOT EXISTS dataset_code_seq",
    ]
    for engine, stmts in ((users_engine, users_stmts), (posts_engine, posts_stmts), (datasets_engine, dataset_stmts)):
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
            for sql in stmts:
                conn.execute(text(sql))
            if stmts is dataset_stmts:        # never hand out a code that already exists
                top = conn.execute(text("SELECT max(substring(dataset_id FROM 4)::bigint) FROM datasets "
                                        "WHERE dataset_id ~ '^ds_[0-9]+$'")).scalar()
                last, called = conn.execute(text("SELECT last_value, is_called FROM dataset_code_seq")).one()
                if top and (top > last or (top == last and not called)):
                    conn.execute(text("SELECT setval('dataset_code_seq', :n, true)"), {"n": top})


_upgrade_schemas()


def _category_banners() -> None:
    """Every category gets its feed-banner scene (only missing / outdated ones are rebuilt)."""
    from app.db.database import PostsSessionLocal
    from app.services import banner_service
    with PostsSessionLocal() as db:
        if banner_service.ensure_all(db):
            try:
                from app.services.cache_service import cache_service
                cache_service.delete("gisviz-cache:categories:list")
            except Exception:
                pass


def _post_activity() -> None:
    """Posts follow their dataset's active state (catalog_service.cascade_posts)."""
    from app.services import catalog_service
    n = catalog_service.sync_post_activity()
    if n:
        print(f"[posts] {n} post(s) switched on / off to match their dataset", flush=True)


try:
    _post_activity()
except Exception as e:          # never block start-up
    print(f"[posts] dataset activity sync skipped: {type(e).__name__}: {e}", flush=True)

try:
    _category_banners()
except Exception as e:          # never block start-up
    print(f"[categories] banner scenes skipped: {type(e).__name__}: {e}", flush=True)


def _repair_post_images() -> None:
    """In the background, shortly after start-up: posts whose image file is missing get it drawn again.
    One worker does it (a 10-minute Redis lock)."""
    import threading
    import time as _t
    from app.services.cache_service import cache_service
    if os.getenv("REPAIR_POST_IMAGES", "1") != "1" or cache_service.hit("gisviz:repair-post-images", 600) > 1:
        return

    def run():
        _t.sleep(int(os.getenv("REPAIR_POST_IMAGES_DELAY", "30")))
        try:
            from app.services.post_service import PostService
            r = PostService().repair_missing_images()
            if r["missing"]:
                print(f"[posts] images: {r['missing']} missing, {r['fixed']} drawn again, {r['failed']} failed", flush=True)
                if r["fixed"]:
                    cache_service.delete_pattern("gisviz-cache:posts:*")
        except Exception as e:
            print(f"[posts] image repair skipped: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=run, daemon=True, name="repair-post-images").start()


try:
    _repair_post_images()
except Exception as e:          # never block start-up
    print(f"[posts] image repair not started: {type(e).__name__}: {e}", flush=True)


def _retire_old_analytics() -> None:
    """One-time move to the slim analytics DB (idempotent, several workers safe):
    1. views recorded in analytics.fact_post_view are copied into posts.post_views (same viewer hash,
       posts.views_count already includes them);
    2. the old star-schema tables nothing reads any more are dropped."""
    from sqlalchemy import text
    from app.db.models import OBSOLETE_ANALYTICS_TABLES
    with analytics_engine.connect() as aconn:
        aconn.execute(text("SELECT pg_advisory_lock(424243)"))
        try:
            has_views = aconn.execute(text("SELECT to_regclass('public.fact_post_view') IS NOT NULL")).scalar()
            if has_views:
                first = aconn.execute(text(
                    "SELECT post_id, viewer_key, first_seen_timestamp FROM fact_post_view "
                    "ORDER BY first_seen_timestamp NULLS LAST, post_id, viewer_key LIMIT 1")).first()
                with posts_engine.begin() as pconn:
                    done = first is None or pconn.execute(text(
                        "SELECT 1 FROM post_views WHERE post_id = :p AND viewer_hash = :v LIMIT 1"),
                        {"p": first.post_id, "v": first.viewer_key}).first() is not None
                    if not done:
                        rows = aconn.execute(text(
                            "SELECT v.post_id, v.user_id, v.viewer_key, "
                            "COALESCE(v.first_seen_timestamp, v.view_date::timestamptz) AS ts "
                            "FROM fact_post_view v")).mappings().all()
                        live = {r[0] for r in pconn.execute(text("SELECT post_id FROM posts"))}
                        batch = [{"p": r["post_id"], "u": r["user_id"], "v": r["viewer_key"], "t": r["ts"]}
                                 for r in rows if r["post_id"] in live]
                        for i in range(0, len(batch), 5000):
                            pconn.execute(text("INSERT INTO post_views (post_id, user_id, viewer_hash, viewed_timestamp) "
                                               "VALUES (:p, :u, :v, :t)"), batch[i:i + 5000])
                        print(f"[analytics] moved {len(batch)} views from fact_post_view to post_views", flush=True)
            for t in OBSOLETE_ANALYTICS_TABLES:
                aconn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
            aconn.commit()
        finally:
            aconn.execute(text("SELECT pg_advisory_unlock(424243)"))
            aconn.commit()


try:
    _retire_old_analytics()
except Exception as e:          # never block start-up; the old tables just stay until the next start
    print(f"[analytics] retiring the old tables skipped: {type(e).__name__}: {e}", flush=True)


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

@app.middleware("http")
async def _errors_keep_cors(request, call_next):
    """Turn unexpected exceptions into a JSON 500 *inside* the CORS layer. Without this, a crash returns
    a bare 500 without CORS headers and the browser only reports "Network Error", hiding the cause."""
    try:
        return await call_next(request)
    except Exception as exc:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": f"Server error: {type(exc).__name__}"}, status_code=500)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),      # list of origins (comma-separated in .env.backend)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Chart/map payloads are large JSON (often 50-500 KB); gzip cuts the transfer ~5-8x.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(auth.router,              prefix=f"{settings.API_VERSION_STR}/auth",       tags=["Authentication"])
app.include_router(users.router,             prefix=f"{settings.API_VERSION_STR}/users",      tags=["Users"])
app.include_router(posts.router,             prefix=f"{settings.API_VERSION_STR}/posts",      tags=["Posts"])
app.include_router(categories.router,        prefix=f"{settings.API_VERSION_STR}/categories", tags=["Categories"])
app.include_router(follows.router,           prefix=f"{settings.API_VERSION_STR}/network",    tags=["Social Graph"])
app.include_router(uploads.router,           prefix=f"{settings.API_VERSION_STR}/uploads",    tags=["Uploads"])
app.include_router(search.router,            prefix=f"{settings.API_VERSION_STR}/search",     tags=["Search"])
app.include_router(admin_endpoints.router,   prefix=f"{settings.API_VERSION_STR}/admin",      tags=["Admin"])
app.include_router(support_endpoints.router, prefix=f"{settings.API_VERSION_STR}/support",    tags=["Support"])
app.include_router(legal_endpoints.router,   prefix=f"{settings.API_VERSION_STR}/legal",      tags=["Legal"])
app.include_router(datasets_endpoints.router, prefix=f"{settings.API_VERSION_STR}/datasets",  tags=["Datasets"])
app.include_router(visuals_endpoints.router, prefix=f"{settings.API_VERSION_STR}/visuals",    tags=["Visuals"])
app.include_router(regions_endpoints.router, prefix=f"{settings.API_VERSION_STR}/regions",    tags=["Regions"])
app.include_router(seo.router,               prefix=f"{settings.API_VERSION_STR}/seo",        tags=["SEO"])
app.include_router(access_endpoints.router,  prefix=f"{settings.API_VERSION_STR}/access",     tags=["Access & sharing"])
app.include_router(orgs_endpoints.router,    prefix=f"{settings.API_VERSION_STR}/orgs",       tags=["Organisations"])

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


# ══════════════════════════════════════════════════════════════════════════════
# Seed — GET /seed writes ALL the seed data below to the databases. Nothing is seeded at start-up.
#   users DB  roles, the development admin          posts DB  post categories (+ description, colour)
#   misc DB   regions, visual categories + types (the chart/map catalog), AI prompt templates
# Re-running is safe: missing rows are added; catalog rows are refreshed from here, but an admin's
# enabled/disabled choice, edited category colours, regions and prompts are never overwritten.
#   development (APP_ENV=development): open, and also creates the dev admin account.
#   production: disabled unless SEED_TOKEN is set; then send header X-Seed-Token: <SEED_TOKEN>.
#               Reference data only (the dev admin with its known password is never created there).
#
# Visual catalog: the 7 categories of the data-to-viz family (Distribution, Correlation, Ranking,
# Part of a whole, Evolution, Map, Flow) merged with the goals of Datylon's "A beginner's guide to
# data visualization".  status: live = renders today | next = small renderer addition | later = new
# data shape or renderer. Only enabled types are offered to publishers and the AI.
# roles: x (placed along), y (measure or "count"), z (series/stack/grid), size, time (play/slider),
#        label (mark name + dropdown FILTER), geometry, source/target (flows), parent (hierarchy).
# ══════════════════════════════════════════════════════════════════════════════

# ── users DB ──────────────────────────────────────────────────────────────────
SEED_ROLES = [
    {"name": "admin",     "permissions": {"manage_tags": True, "moderate": True, "publish": True, "admin": True}},
    {"name": "editor",    "permissions": {"manage_tags": True, "moderate": True, "publish": True}},
    {"name": "publisher", "permissions": {"publish": True}},
    {"name": "viewer",    "permissions": {}},
    {"name": "support",   "permissions": {"view_reports": True, "publish": True}},
]

# development only (GET /seed with APP_ENV=development); change the password after first login
SEED_DEV_ADMIN = {
    "user_handle": "system_admin", "email_address": "admin@gisviz.com", "password": "Password123!",
    "title": "Lead Architect", "role": "admin",
    "location": {"place": "Boulder", "state": "Colorado", "country": "United States"},
}

# ── posts DB ──────────────────────────────────────────────────────────────────
SEED_POST_CATEGORIES = [
    {"slug": "climate", "label": "Climate", "theme_color": "#f97316",
     "description": "Explore global temperature anomalies, carbon emissions, sea-level rise forecasts, and climate impact models."},
    {"slug": "infrastructure", "label": "Infrastructure", "theme_color": "#3b82f6",
     "description": "Map critical networks spanning energy grids, transport hubs, telecommunications, and civic facilities."},
    {"slug": "demographics", "label": "Demographics", "theme_color": "#a855f7",
     "description": "Visualize population distributions, census metrics, socioeconomic trends, and human geography."},
    {"slug": "transport", "label": "Transport", "theme_color": "#10b981",
     "description": "Track mobility patterns, logistics flows, transit networks, and global supply chain telemetry."},
    {"slug": "environment", "label": "Environment", "theme_color": "#22c55e",
     "description": "Monitor ecosystems, land use changes, hydrological basins, and conservation efforts."},
    {"slug": "economy", "label": "Economy", "theme_color": "#6366f1",
     "description": "Analyze global trade routes, real estate markets, financial zones, and macroeconomic indicators."},
    {"slug": "urban-planning", "label": "Urban Planning", "theme_color": "#f43f5e",
     "description": "Inspect zoning boundaries, smart city metrics, urban expansion, and parcel-level civic data."},
]

# ── misc DB: regions ──────────────────────────────────────────────────────────
# (code, name, kind, parent_code, m49, bbox)
SEED_REGIONS = [
    # The 8 top-level regions (the only ones in the feed / datasets filters). Countries sit under them in SEED_COUNTRIES.
    ("global", "Global", "global", None, "001", [-180, -90, 180, 90]),
    ("africa", "Africa", "continent", "global", "002", [-25.4, -34.9, 63.5, 37.6]),
    ("antarctica", "Antarctica", "continent", "global", "010", [-180, -90, 180, -60]),
    ("asia", "Asia", "continent", "global", "142", [25, -11, 180, 82]),
    ("europe", "Europe", "continent", "global", "150", [-31.3, 34.5, 69, 81.9]),
    ("north-america", "North America", "continent", "global", "003", [-170, 7, -50, 84]),
    ("oceania", "Oceania", "continent", "global", "009", [110, -53, 180, 0]),
    ("south-america", "South America", "continent", "global", "005", [-82, -56, -34, 13]),
]


# Countries = sub-regions of a continent (regions.kind="country", parent_code=<continent>); ISO 3166-1 (249 entries).
# code = ISO alpha-2 lower-case; m49 = ISO numeric (also the id used by world-atlas / Natural Earth shapes).
# bbox = [west, south, east, north] from Natural Earth 1:50m; a country crossing 180° runs one side past ±180
# (e.g. Russia 19.6 -> 190.27, United States -187.51 -> -66.99), which map libraries accept. None = too small to be in the 1:50m set.
SEED_COUNTRIES = [
    ("dz", "Algeria", "africa", "DZA", "012", [-8.69, 18.99, 11.97, 37.09]),
    ("ao", "Angola", "africa", "AGO", "024", [11.74, -18.02, 24.05, -4.43]),
    ("bj", "Benin", "africa", "BEN", "204", [0.77, 6.22, 3.84, 12.38]),
    ("bw", "Botswana", "africa", "BWA", "072", [19.98, -26.85, 29.36, -17.79]),
    ("bf", "Burkina Faso", "africa", "BFA", "854", [-5.52, 9.43, 2.39, 15.08]),
    ("bi", "Burundi", "africa", "BDI", "108", [29.01, -4.46, 30.81, -2.31]),
    ("cv", "Cabo Verde", "africa", "CPV", "132", [-25.34, 14.82, -22.68, 17.19]),
    ("cm", "Cameroon", "africa", "CMR", "120", [8.53, 1.68, 16.18, 13.08]),
    ("cf", "Central African Republic", "africa", "CAF", "140", [14.43, 2.27, 27.4, 11]),
    ("td", "Chad", "africa", "TCD", "148", [13.45, 7.48, 23.98, 23.45]),
    ("km", "Comoros", "africa", "COM", "174", [43.23, -12.37, 44.53, -11.37]),
    ("ci", "Côte d'Ivoire", "africa", "CIV", "384", [-8.6, 4.35, -2.51, 10.72]),
    ("cd", "DR Congo", "africa", "COD", "180", [12.21, -13.45, 31.28, 5.31]),
    ("dj", "Djibouti", "africa", "DJI", "262", [41.77, 10.94, 43.41, 12.71]),
    ("eg", "Egypt", "africa", "EGY", "818", [24.7, 21.99, 36.87, 31.65]),
    ("gq", "Equatorial Guinea", "africa", "GNQ", "226", [8.43, 0.96, 11.33, 3.76]),
    ("er", "Eritrea", "africa", "ERI", "232", [36.43, 12.38, 43.12, 18]),
    ("sz", "Eswatini", "africa", "SWZ", "748", [30.79, -27.31, 32.11, -25.74]),
    ("et", "Ethiopia", "africa", "ETH", "231", [33, 3.46, 47.98, 14.85]),
    ("ga", "Gabon", "africa", "GAB", "266", [8.7, -3.92, 14.48, 2.3]),
    ("gm", "Gambia", "africa", "GMB", "270", [-16.82, 13.06, -13.83, 13.81]),
    ("gh", "Ghana", "africa", "GHA", "288", [-3.25, 4.76, 1.19, 11.17]),
    ("gn", "Guinea", "africa", "GIN", "324", [-15.05, 7.22, -7.68, 12.67]),
    ("gw", "Guinea-Bissau", "africa", "GNB", "624", [-16.71, 10.94, -13.67, 12.68]),
    ("ke", "Kenya", "africa", "KEN", "404", [33.9, -4.69, 41.88, 5.49]),
    ("ls", "Lesotho", "africa", "LSO", "426", [27.05, -30.64, 29.39, -28.58]),
    ("lr", "Liberia", "africa", "LBR", "430", [-11.51, 4.35, -7.4, 8.54]),
    ("ly", "Libya", "africa", "LBY", "434", [9.31, 19.5, 25.15, 33.18]),
    ("mg", "Madagascar", "africa", "MDG", "450", [43.26, -25.57, 50.48, -12.08]),
    ("mw", "Malawi", "africa", "MWI", "454", [32.67, -17.13, 35.89, -9.39]),
    ("ml", "Mali", "africa", "MLI", "466", [-12.28, 10.14, 4.24, 25]),
    ("mr", "Mauritania", "africa", "MRT", "478", [-17.06, 14.74, -4.82, 27.29]),
    ("mu", "Mauritius", "africa", "MUS", "480", [57.32, -20.51, 57.79, -19.99]),
    ("yt", "Mayotte", "africa", "MYT", "175", None),
    ("ma", "Morocco", "africa", "MAR", "504", [-17, 21.42, -1.06, 35.93]),
    ("mz", "Mozambique", "africa", "MOZ", "508", [30.22, -26.86, 40.84, -10.46]),
    ("na", "Namibia", "africa", "NAM", "516", [11.72, -28.94, 25.26, -16.97]),
    ("ne", "Niger", "africa", "NER", "562", [0.16, 11.7, 15.96, 23.52]),
    ("ng", "Nigeria", "africa", "NGA", "566", [2.69, 4.28, 14.63, 13.87]),
    ("cg", "Republic of the Congo", "africa", "COG", "178", [11.13, -5, 18.62, 3.69]),
    ("rw", "Rwanda", "africa", "RWA", "646", [28.86, -2.81, 30.88, -1.06]),
    ("re", "Réunion", "africa", "REU", "638", None),
    ("sh", "Saint Helena", "africa", "SHN", "654", [-14.42, -16, -5.66, -7.88]),
    ("st", "Sao Tome and Principe", "africa", "STP", "678", [6.47, 0.05, 7.45, 1.7]),
    ("sn", "Senegal", "africa", "SEN", "686", [-17.53, 12.33, -11.38, 16.68]),
    ("sc", "Seychelles", "africa", "SYC", "690", [55.38, -4.79, 55.54, -4.56]),
    ("sl", "Sierra Leone", "africa", "SLE", "694", [-13.29, 6.91, -10.28, 10]),
    ("so", "Somalia", "africa", "SOM", "706", [40.96, -1.7, 51.39, 11.98]),
    ("za", "South Africa", "africa", "ZAF", "710", [16.45, -46.96, 37.89, -22.15]),
    ("ss", "South Sudan", "africa", "SSD", "728", [24.15, 3.49, 35.27, 12.22]),
    ("sd", "Sudan", "africa", "SDN", "729", [21.83, 8.67, 38.61, 22.2]),
    ("tz", "Tanzania", "africa", "TZA", "834", [29.32, -11.72, 40.46, -0.99]),
    ("tg", "Togo", "africa", "TGO", "768", [-0.09, 6.09, 1.78, 11.12]),
    ("tn", "Tunisia", "africa", "TUN", "788", [7.5, 30.23, 11.54, 37.34]),
    ("ug", "Uganda", "africa", "UGA", "800", [29.56, -1.47, 34.98, 4.22]),
    ("eh", "Western Sahara", "africa", "ESH", "732", [-17.1, 20.81, -8.68, 27.66]),
    ("zm", "Zambia", "africa", "ZMB", "894", [21.98, -18.04, 33.66, -8.19]),
    ("zw", "Zimbabwe", "africa", "ZWE", "716", [25.22, -22.4, 33.01, -15.64]),
    ("aq", "Antarctica", "antarctica", "ATA", "010", [-180, -90, 180, -60.52]),
    ("bv", "Bouvet Island", "antarctica", "BVT", "074", None),
    ("tf", "French Southern Territories", "antarctica", "ATF", "260", [51.66, -49.71, 70.56, -46.33]),
    ("hm", "Heard Island and McDonald Islands", "antarctica", "HMD", "334", [73.25, -53.19, 73.84, -52.97]),
    ("gs", "South Georgia and the South Sandwich Islands", "antarctica", "SGS", "239", [-38.02, -58.49, -26.26, -53.98]),
    ("af", "Afghanistan", "asia", "AFG", "004", [60.49, 29.39, 74.89, 38.46]),
    ("am", "Armenia", "asia", "ARM", "051", [43.44, 38.87, 46.59, 41.29]),
    ("az", "Azerbaijan", "asia", "AZE", "031", [44.77, 38.4, 50.37, 41.89]),
    ("bh", "Bahrain", "asia", "BHR", "048", [50.45, 25.81, 50.62, 26.25]),
    ("bd", "Bangladesh", "asia", "BGD", "050", [88.02, 20.79, 92.63, 26.57]),
    ("bt", "Bhutan", "asia", "BTN", "064", [88.74, 26.7, 92.08, 28.31]),
    ("io", "British Indian Ocean Territory", "asia", "IOT", "086", [72.35, -7.43, 72.5, -7.22]),
    ("bn", "Brunei", "asia", "BRN", "096", [114.07, 4.02, 115.33, 5.02]),
    ("kh", "Cambodia", "asia", "KHM", "116", [102.32, 10.41, 107.61, 14.7]),
    ("cn", "China", "asia", "CHN", "156", [73.61, 18.22, 134.75, 53.56]),
    ("cx", "Christmas Island", "asia", "CXR", "162", None),
    ("cc", "Cocos (Keeling) Islands", "asia", "CCK", "166", None),
    ("cy", "Cyprus", "asia", "CYP", "196", [32.3, 34.57, 34.05, 35.18]),
    ("ge", "Georgia", "asia", "GEO", "268", [39.98, 41.07, 46.67, 43.57]),
    ("hk", "Hong Kong", "asia", "HKG", "344", [113.84, 22.2, 114.34, 22.57]),
    ("in", "India", "asia", "IND", "356", [68.16, 6.75, 97.34, 35.5]),
    ("id", "Indonesia", "asia", "IDN", "360", [95.21, -10.91, 140.98, 5.91]),
    ("ir", "Iran", "asia", "IRN", "364", [44.02, 25.1, 63.3, 39.77]),
    ("iq", "Iraq", "asia", "IRQ", "368", [38.77, 29.06, 48.54, 37.37]),
    ("il", "Israel", "asia", "ISR", "376", [34.25, 29.48, 35.91, 33.43]),
    ("jp", "Japan", "asia", "JPN", "392", [123.68, 24.27, 145.83, 45.51]),
    ("jo", "Jordan", "asia", "JOR", "400", [34.95, 29.19, 39.29, 33.37]),
    ("kz", "Kazakhstan", "asia", "KAZ", "398", [46.61, 40.61, 87.32, 55.39]),
    ("kw", "Kuwait", "asia", "KWT", "414", [46.53, 28.53, 48.44, 30.1]),
    ("kg", "Kyrgyzstan", "asia", "KGZ", "417", [69.23, 39.21, 80.25, 43.24]),
    ("la", "Laos", "asia", "LAO", "418", [100.12, 13.92, 107.65, 22.5]),
    ("lb", "Lebanon", "asia", "LBN", "422", [35.11, 33.07, 36.59, 34.68]),
    ("mo", "Macao", "asia", "MAC", "446", [113.48, 22.2, 113.55, 22.25]),
    ("my", "Malaysia", "asia", "MYS", "458", [99.65, 0.86, 119.27, 7.35]),
    ("mv", "Maldives", "asia", "MDV", "462", [73.38, 3.23, 73.53, 4.25]),
    ("mn", "Mongolia", "asia", "MNG", "496", [87.74, 41.6, 119.9, 52.12]),
    ("mm", "Myanmar", "asia", "MMR", "104", [92.18, 9.88, 101.15, 28.52]),
    ("np", "Nepal", "asia", "NPL", "524", [80.05, 26.36, 88.16, 30.39]),
    ("kp", "North Korea", "asia", "PRK", "408", [124.35, 37.72, 130.69, 43]),
    ("om", "Oman", "asia", "OMN", "512", [51.98, 16.65, 59.84, 26.36]),
    ("pk", "Pakistan", "asia", "PAK", "586", [60.84, 23.75, 77.05, 37.04]),
    ("ps", "Palestine", "asia", "PSE", "275", [34.2, 31.21, 35.57, 32.54]),
    ("ph", "Philippines", "asia", "PHL", "608", [116.97, 5.06, 126.59, 20.84]),
    ("qa", "Qatar", "asia", "QAT", "634", [50.76, 24.57, 51.61, 26.15]),
    ("sa", "Saudi Arabia", "asia", "SAU", "682", [34.62, 16.37, 55.64, 32.13]),
    ("sg", "Singapore", "asia", "SGP", "702", [103.65, 1.27, 104, 1.45]),
    ("kr", "South Korea", "asia", "KOR", "410", [126.01, 33.2, 130.94, 38.62]),
    ("lk", "Sri Lanka", "asia", "LKA", "144", [79.71, 5.95, 81.88, 9.81]),
    ("sy", "Syria", "asia", "SYR", "760", [35.76, 32.32, 42.36, 37.3]),
    ("tw", "Taiwan", "asia", "TWN", "158", [118.29, 21.92, 121.93, 25.28]),
    ("tj", "Tajikistan", "asia", "TJK", "762", [67.35, 36.68, 75.12, 41.03]),
    ("th", "Thailand", "asia", "THA", "764", [97.38, 5.64, 105.64, 20.42]),
    ("tl", "Timor-Leste", "asia", "TLS", "626", [124.04, -9.51, 127.3, -8.14]),
    ("tm", "Turkmenistan", "asia", "TKM", "795", [52.49, 35.17, 66.63, 42.78]),
    ("tr", "Türkiye", "asia", "TUR", "792", [25.67, 35.83, 44.82, 42.09]),
    ("ae", "United Arab Emirates", "asia", "ARE", "784", [51.57, 22.62, 56.39, 26.07]),
    ("uz", "Uzbekistan", "asia", "UZB", "860", [55.98, 37.17, 73.14, 45.56]),
    ("vn", "Vietnam", "asia", "VNM", "704", [102.13, 8.58, 109.45, 23.34]),
    ("ye", "Yemen", "asia", "YEM", "887", [42.55, 12.32, 54.51, 19]),
    ("al", "Albania", "europe", "ALB", "008", [19.28, 39.65, 21.03, 42.65]),
    ("ad", "Andorra", "europe", "AND", "020", [1.42, 42.43, 1.74, 42.64]),
    ("at", "Austria", "europe", "AUT", "040", [9.52, 46.4, 17.15, 49]),
    ("by", "Belarus", "europe", "BLR", "112", [23.18, 51.26, 32.71, 56.15]),
    ("be", "Belgium", "europe", "BEL", "056", [2.53, 49.51, 6.36, 51.49]),
    ("ba", "Bosnia and Herzegovina", "europe", "BIH", "070", [15.74, 42.56, 19.58, 45.28]),
    ("bg", "Bulgaria", "europe", "BGR", "100", [22.34, 41.24, 28.59, 44.24]),
    ("hr", "Croatia", "europe", "HRV", "191", [13.52, 42.43, 19.4, 46.53]),
    ("cz", "Czechia", "europe", "CZE", "203", [12.09, 48.58, 18.83, 51.04]),
    ("dk", "Denmark", "europe", "DNK", "208", [8.12, 54.63, 15.14, 57.74]),
    ("ee", "Estonia", "europe", "EST", "233", [21.85, 57.52, 28.15, 59.64]),
    ("fo", "Faroe Islands", "europe", "FRO", "234", [-7.42, 61.41, -6.41, 62.36]),
    ("fi", "Finland", "europe", "FIN", "246", [20.62, 59.82, 31.54, 70.06]),
    ("fr", "France", "europe", "FRA", "250", [-61.79, -21.37, 55.84, 51.1]),
    ("de", "Germany", "europe", "DEU", "276", [5.86, 47.28, 15.02, 55.06]),
    ("gi", "Gibraltar", "europe", "GIB", "292", None),
    ("gr", "Greece", "europe", "GRC", "300", [19.65, 34.93, 28.23, 41.74]),
    ("gg", "Guernsey", "europe", "GGY", "831", [-2.65, 49.43, -2.51, 49.51]),
    ("hu", "Hungary", "europe", "HUN", "348", [16.09, 45.75, 22.88, 48.55]),
    ("is", "Iceland", "europe", "ISL", "352", [-24.47, 63.41, -13.56, 66.53]),
    ("ie", "Ireland", "europe", "IRL", "372", [-10.39, 51.47, -6.03, 55.37]),
    ("im", "Isle of Man", "europe", "IMN", "833", [-4.79, 54.06, -4.34, 54.41]),
    ("it", "Italy", "europe", "ITA", "380", [6.63, 36.69, 18.48, 47.08]),
    ("je", "Jersey", "europe", "JEY", "832", [-2.24, 49.17, -2.01, 49.27]),
    ("lv", "Latvia", "europe", "LVA", "428", [21.02, 55.67, 28.2, 58.06]),
    ("li", "Liechtenstein", "europe", "LIE", "438", [9.48, 47.06, 9.61, 47.27]),
    ("lt", "Lithuania", "europe", "LTU", "440", [20.9, 53.89, 26.78, 56.41]),
    ("lu", "Luxembourg", "europe", "LUX", "442", [5.73, 49.45, 6.49, 50.17]),
    ("mt", "Malta", "europe", "MLT", "470", [14.18, 35.82, 14.57, 36.08]),
    ("md", "Moldova", "europe", "MDA", "498", [26.62, 45.45, 30.13, 48.48]),
    ("mc", "Monaco", "europe", "MCO", "492", [7.38, 43.73, 7.44, 43.77]),
    ("me", "Montenegro", "europe", "MNE", "499", [18.44, 41.87, 20.35, 43.54]),
    ("nl", "Netherlands", "europe", "NLD", "528", [-68.37, 12.03, 7.2, 53.63]),
    ("mk", "North Macedonia", "europe", "MKD", "807", [20.45, 40.85, 23.01, 42.36]),
    ("no", "Norway", "europe", "NOR", "578", [-9.1, 58.02, 33.63, 80.48]),
    ("pl", "Poland", "europe", "POL", "616", [14.13, 49.02, 24.1, 54.84]),
    ("pt", "Portugal", "europe", "PRT", "620", [-31.28, 32.65, -6.21, 42.14]),
    ("ro", "Romania", "europe", "ROU", "642", [20.24, 43.67, 29.71, 48.26]),
    ("ru", "Russia", "europe", "RUS", "643", [19.6, 41.2, 190.27, 81.85]),
    ("sm", "San Marino", "europe", "SMR", "674", [12.4, 43.89, 12.52, 43.99]),
    ("rs", "Serbia", "europe", "SRB", "688", [18.84, 42.24, 22.98, 46.17]),
    ("sk", "Slovakia", "europe", "SVK", "703", [16.86, 47.76, 22.54, 49.6]),
    ("si", "Slovenia", "europe", "SVN", "705", [13.38, 45.43, 16.52, 46.86]),
    ("es", "Spain", "europe", "ESP", "724", [-18.16, 27.65, 4.32, 43.77]),
    ("sj", "Svalbard and Jan Mayen", "europe", "SJM", "744", None),
    ("se", "Sweden", "europe", "SWE", "752", [11.15, 55.35, 24.15, 69.04]),
    ("ch", "Switzerland", "europe", "CHE", "756", [5.97, 45.83, 10.46, 47.78]),
    ("ua", "Ukraine", "europe", "UKR", "804", [22.13, 45.23, 40.13, 52.35]),
    ("gb", "United Kingdom", "europe", "GBR", "826", [-8.15, 50.02, 1.75, 60.83]),
    ("va", "Vatican City", "europe", "VAT", "336", [12.43, 41.9, 12.44, 41.91]),
    ("ax", "Åland Islands", "europe", "ALA", "248", [19.52, 60.01, 20.61, 60.41]),
    ("ai", "Anguilla", "north-america", "AIA", "660", [-63.16, 18.17, -62.98, 18.27]),
    ("ag", "Antigua and Barbuda", "north-america", "ATG", "028", [-61.89, 17, -61.68, 17.71]),
    ("aw", "Aruba", "north-america", "ABW", "533", [-70.07, 12.42, -69.9, 12.61]),
    ("bs", "Bahamas", "north-america", "BHS", "044", [-78.99, 20.94, -72.75, 26.94]),
    ("bb", "Barbados", "north-america", "BRB", "052", [-59.65, 13.06, -59.43, 13.32]),
    ("bz", "Belize", "north-america", "BLZ", "084", [-89.24, 15.89, -87.79, 18.48]),
    ("bm", "Bermuda", "north-america", "BMU", "060", [-64.86, 32.26, -64.67, 32.39]),
    ("vg", "British Virgin Islands", "north-america", "VGB", "092", [-64.69, 18.4, -64.27, 18.75]),
    ("ca", "Canada", "north-america", "CAN", "124", [-141, 41.68, -52.65, 83.12]),
    ("bq", "Caribbean Netherlands", "north-america", "BES", "535", None),
    ("ky", "Cayman Islands", "north-america", "CYM", "136", [-81.42, 19.27, -79.74, 19.77]),
    ("cr", "Costa Rica", "north-america", "CRI", "188", [-85.91, 8.07, -82.57, 11.19]),
    ("cu", "Cuba", "north-america", "CUB", "192", [-84.89, 19.86, -74.14, 23.19]),
    ("cw", "Curaçao", "north-america", "CUW", "531", [-69.16, 12.05, -68.75, 12.38]),
    ("dm", "Dominica", "north-america", "DMA", "212", [-61.48, 15.23, -61.25, 15.63]),
    ("do", "Dominican Republic", "north-america", "DOM", "214", [-72, 17.63, -68.34, 19.91]),
    ("sv", "El Salvador", "north-america", "SLV", "222", [-90.11, 13.16, -87.72, 14.43]),
    ("gl", "Greenland", "north-america", "GRL", "304", [-72.82, 59.81, -11.42, 83.6]),
    ("gd", "Grenada", "north-america", "GRD", "308", [-61.78, 12.01, -61.61, 12.24]),
    ("gp", "Guadeloupe", "north-america", "GLP", "312", None),
    ("gt", "Guatemala", "north-america", "GTM", "320", [-92.23, 13.74, -88.23, 17.82]),
    ("ht", "Haiti", "north-america", "HTI", "332", [-74.48, 18.04, -71.65, 20.09]),
    ("hn", "Honduras", "north-america", "HND", "340", [-89.36, 12.98, -83.16, 16.51]),
    ("jm", "Jamaica", "north-america", "JAM", "388", [-78.34, 17.71, -76.21, 18.52]),
    ("mq", "Martinique", "north-america", "MTQ", "474", None),
    ("mx", "Mexico", "north-america", "MEX", "484", [-118.4, 14.54, -86.7, 32.72]),
    ("ms", "Montserrat", "north-america", "MSR", "500", [-62.22, 16.68, -62.15, 16.81]),
    ("ni", "Nicaragua", "north-america", "NIC", "558", [-87.67, 10.74, -83.16, 15.01]),
    ("pa", "Panama", "north-america", "PAN", "591", [-83.03, 7.22, -77.2, 9.6]),
    ("pr", "Puerto Rico", "north-america", "PRI", "630", [-67.94, 17.95, -65.3, 18.52]),
    ("bl", "Saint Barthélemy", "north-america", "BLM", "652", [-62.88, 17.87, -62.8, 17.92]),
    ("kn", "Saint Kitts and Nevis", "north-america", "KNA", "659", [-62.84, 17.1, -62.53, 17.4]),
    ("lc", "Saint Lucia", "north-america", "LCA", "662", [-61.07, 13.72, -60.89, 14.09]),
    ("mf", "Saint Martin (French part)", "north-america", "MAF", "663", [-63.12, 18.07, -63.01, 18.12]),
    ("pm", "Saint Pierre and Miquelon", "north-america", "SPM", "666", [-56.39, 46.75, -56.14, 47.1]),
    ("vc", "Saint Vincent and the Grenadines", "north-america", "VCT", "670", [-61.35, 12.69, -61.12, 13.36]),
    ("sx", "Sint Maarten (Dutch part)", "north-america", "SXM", "534", [-63.12, 18.02, -63.01, 18.07]),
    ("tt", "Trinidad and Tobago", "north-america", "TTO", "780", [-61.9, 10.06, -60.53, 11.32]),
    ("tc", "Turks and Caicos Islands", "north-america", "TCA", "796", [-72.34, 21.75, -71.64, 21.95]),
    ("vi", "U.S. Virgin Islands", "north-america", "VIR", "850", [-65.02, 17.7, -64.58, 18.38]),
    ("us", "United States", "north-america", "USA", "840", [-187.51, 18.96, -66.99, 71.41]),
    ("as", "American Samoa", "oceania", "ASM", "016", [-170.82, -14.36, -170.57, -14.26]),
    ("au", "Australia", "oceania", "AUS", "036", [123.57, -12.44, 123.59, -12.42]),
    ("ck", "Cook Islands", "oceania", "COK", "184", [-159.84, -21.25, -159.74, -21.19]),
    ("fj", "Fiji", "oceania", "FJI", "242", [174.59, -21.71, 181.75, -12.48]),
    ("pf", "French Polynesia", "oceania", "PYF", "258", [-151.51, -20.88, -136.3, -8.78]),
    ("gu", "Guam", "oceania", "GUM", "316", [144.65, 13.26, 144.94, 13.62]),
    ("ki", "Kiribati", "oceania", "KIR", "296", [-190.48, -11.46, -151.78, 3.92]),
    ("mh", "Marshall Islands", "oceania", "MHL", "584", [166.85, 5.8, 171.76, 11.17]),
    ("fm", "Micronesia", "oceania", "FSM", "583", [138.06, 5.28, 162.99, 9.59]),
    ("nr", "Nauru", "oceania", "NRU", "520", [166.91, -0.55, 166.96, -0.49]),
    ("nc", "New Caledonia", "oceania", "NCL", "540", [159.93, -22.66, 168.14, -19.11]),
    ("nz", "New Zealand", "oceania", "NZL", "554", [165.89, -52.57, 188.81, -8.55]),
    ("nu", "Niue", "oceania", "NIU", "570", [-169.95, -19.14, -169.79, -18.97]),
    ("nf", "Norfolk Island", "oceania", "NFK", "574", [167.91, -29.1, 167.99, -29.01]),
    ("mp", "Northern Mariana Islands", "oceania", "MNP", "580", [145.15, 14.11, 145.84, 18.81]),
    ("pw", "Palau", "oceania", "PLW", "585", [131.14, 3.02, 134.66, 7.71]),
    ("pg", "Papua New Guinea", "oceania", "PNG", "598", [140.86, -11.63, 155.96, -1.35]),
    ("pn", "Pitcairn", "oceania", "PCN", "612", [-128.35, -24.41, -128.29, -24.32]),
    ("ws", "Samoa", "oceania", "WSM", "882", [-172.78, -14.05, -171.45, -13.47]),
    ("sb", "Solomon Islands", "oceania", "SLB", "090", [155.68, -11.83, 166.93, -6.61]),
    ("tk", "Tokelau", "oceania", "TKL", "772", None),
    ("to", "Tonga", "oceania", "TON", "776", [-175.36, -21.45, -173.92, -18.57]),
    ("tv", "Tuvalu", "oceania", "TUV", "798", None),
    ("um", "U.S. Minor Outlying Islands", "oceania", "UMI", "581", None),
    ("vu", "Vanuatu", "oceania", "VUT", "548", [166.53, -20.24, 169.89, -13.71]),
    ("wf", "Wallis and Futuna", "oceania", "WLF", "876", [-178.19, -14.32, -176.13, -13.22]),
    ("ar", "Argentina", "south-america", "ARG", "032", [-73.58, -55.03, -53.67, -21.8]),
    ("bo", "Bolivia", "south-america", "BOL", "068", [-69.64, -22.89, -57.49, -9.71]),
    ("br", "Brazil", "south-america", "BRA", "076", [-74, -33.74, -34.81, 5.26]),
    ("cl", "Chile", "south-america", "CHL", "152", [-109.44, -55.89, -66.44, -17.51]),
    ("co", "Colombia", "south-america", "COL", "170", [-79.03, -4.24, -66.88, 12.43]),
    ("ec", "Ecuador", "south-america", "ECU", "218", [-91.66, -4.99, -75.25, 1.46]),
    ("fk", "Falkland Islands", "south-america", "FLK", "238", [-61.14, -52.31, -57.79, -51.27]),
    ("gf", "French Guiana", "south-america", "GUF", "254", None),
    ("gy", "Guyana", "south-america", "GUY", "328", [-61.39, 1.2, -56.48, 8.55]),
    ("py", "Paraguay", "south-america", "PRY", "600", [-62.65, -27.55, -54.24, -19.29]),
    ("pe", "Peru", "south-america", "PER", "604", [-81.34, -18.35, -68.69, -0.04]),
    ("sr", "Suriname", "south-america", "SUR", "740", [-58.06, 1.84, -53.99, 5.99]),
    ("uy", "Uruguay", "south-america", "URY", "858", [-58.44, -34.93, -53.12, -30.1]),
    ("ve", "Venezuela", "south-america", "VEN", "862", [-73.37, 0.69, -59.83, 12.18]),
]


# ── misc DB: visual catalog — categories (colours = the image's family colours) ─────────────────────────
SEED_VISUAL_CATEGORIES = [
    {"code": "distribution", "name": "Distribution", "color": "#E8B33C", "sort": 1,
     "question": "How are the values spread out? Where do most fall, are there outliers?",
     "guide_goals": ["Distribution"]},
    {"code": "correlation", "name": "Correlation", "color": "#9A9A9A", "sort": 2,
     "question": "Do two or more numeric variables move together?",
     "guide_goals": ["Correlation"]},
    {"code": "ranking", "name": "Ranking & comparison", "color": "#9BC45A", "sort": 3,
     "question": "Which categories are bigger or smaller, and by how much?",
     "guide_goals": ["Comparison"]},
    {"code": "part_of_whole", "name": "Part of a whole", "color": "#E8636A", "sort": 4,
     "question": "How do the parts add up to a total, or nest into levels?",
     "guide_goals": ["Part-to-whole", "Hierarchy"]},
    {"code": "evolution", "name": "Evolution", "color": "#5C86C8", "sort": 5,
     "question": "How does it change over time?",
     "guide_goals": ["Data over time"]},
    {"code": "map", "name": "Map", "color": "#9468AE", "sort": 6,
     "question": "Where does it happen, and how does it differ by place?",
     "guide_goals": ["Geospatial"]},
    {"code": "flow", "name": "Flow & relationship", "color": "#A2324E", "sort": 7,
     "question": "What moves between things, or how are things connected?",
     "guide_goals": ["Flow", "Relationship"]},
]

# Interactions a type can offer (the renderer implements each once, for every type that lists it)
SEED_INTERACTIONS = {
    "label_filter": "Pick labels from a dropdown to show only those (replaces switching chart types)",
    "time_play":    "Play button + slider that steps through the time column",
    "measure_pick": "Switch the numeric column being shown",
    "zoom":         "Pan / zoom (maps, large scatters)",
    "tooltip":      "Hover / tap for the exact values",
}

_L, _N, _R = "live", "next", "later"


def _vt(code, name, category, kind, roles, best_when, avoid_when, status, interactions=("tooltip", "label_filter"),
      legacy=None):
    return {"code": code, "name": name, "category": category, "kind": kind, "roles": roles,
            "best_when": best_when, "avoid_when": avoid_when, "status": status,
            "interactions": list(interactions), "legacy_code": legacy}


# ── visual types ──────────────────────────────────────────────────────────────
SEED_VISUAL_TYPES = [
    # Distribution
    _vt("histogram", "Histogram", "distribution", "chart", {"y": "numeric"},
      "One numeric variable: see its shape, peaks and gaps (frequency in bins).",
      "Fewer than ~30 values, or comparing many groups.", _L, ("tooltip",), legacy="histogram"),
    _vt("density", "Density plot", "distribution", "chart", {"y": "numeric", "z?": "categorical"},
      "Smooth shape of a distribution; overlay 2–3 groups.", "Small samples (the curve invents detail).", _N),
    _vt("boxplot", "Box plot", "distribution", "chart", {"x": "categorical", "y": "numeric"},
      "Compare median, spread and outliers across groups.", "Audiences unfamiliar with quartiles.", _N),
    _vt("violin", "Violin plot", "distribution", "chart", {"x": "categorical", "y": "numeric"},
      "Like a box plot but also shows the shape (two peaks etc.).", "Few points per group.", _R),
    _vt("ridgeline", "Ridgeline", "distribution", "chart", {"x": "categorical", "y": "numeric"},
      "Many groups' distributions stacked, e.g. by month or country.", "Only 1–2 groups.", _R),
    _vt("strip_plot", "Strip / beeswarm plot", "distribution", "chart", {"x?": "categorical", "y": "numeric", "label": "any"},
      "Every individual point visible; small to medium datasets.", "Thousands of points (overplotting).", _N),

    # Correlation
    _vt("scatter", "Scatter plot", "correlation", "chart", {"x": "numeric", "y": "numeric", "label?": "any"},
      "Relationship between two numeric variables; spot clusters and outliers.", "One variable is categorical.", _L,
      ("tooltip", "label_filter", "zoom"), legacy="scatter"),
    _vt("bubble", "Bubble chart", "correlation", "chart", {"x": "numeric", "y": "numeric", "size": "numeric", "label?": "any"},
      "Three numeric variables (the third as size).", "Precise reading of the size variable matters.", _L,
      ("tooltip", "label_filter", "time_play"), legacy="bubble"),
    _vt("heatmap", "Heatmap (grid)", "correlation", "chart", {"x": "categorical", "z": "categorical", "y": "numeric"},
      "A measure across two categories (e.g. weekday × hour, country × year).", "Exact values must be compared.", _L,
      legacy="heatmap"),
    _vt("correlogram", "Correlogram", "correlation", "chart", {"measures": "numeric[3+]"},
      "Pairwise correlation of many numeric columns at once.", "General audiences.", _R, ("tooltip",)),
    _vt("connected_scatter", "Connected scatter", "correlation", "chart", {"x": "numeric", "y": "numeric", "time": "temporal"},
      "How two measures moved together over time.", "Many series (becomes spaghetti).", _N, ("tooltip", "time_play")),
    _vt("density_2d", "2D density / hexbin", "correlation", "chart", {"x": "numeric", "y": "numeric"},
      "Scatter with too many points to see.", "Small datasets.", _R, ("tooltip", "zoom")),

    # Ranking & comparison
    _vt("bar", "Column chart (vertical bars)", "ranking", "chart", {"x": "categorical|ordinal", "y": "numeric|count"},
      "Few categories, or ordered ones (age groups, years).", "Long labels or more than ~12 bars.", _L, legacy="bar"),
    _vt("hbar", "Bar chart (horizontal)", "ranking", "chart", {"x": "categorical", "y": "numeric|count"},
      "Ranking many categories with long names (countries, products).", "Time on the axis.", _L, legacy="hbar"),
    _vt("grouped_bar", "Grouped bar chart", "ranking", "chart", {"x": "categorical", "z": "categorical", "y": "numeric"},
      "Compare 2–3 values per category side by side.", "More than ~4 series.", _N),
    _vt("lollipop", "Lollipop / dot plot", "ranking", "chart", {"x": "categorical", "y": "numeric"},
      "Many categories; lighter than bars.", "Values must be read precisely from length.", _N),
    _vt("dumbbell", "Dumbbell chart", "ranking", "chart", {"x": "categorical", "y": "numeric", "z": "categorical[2]"},
      "Two values per category: before/after, men/women, gap.", "More than two values per category.", _N),
    _vt("slope", "Slope chart", "ranking", "chart", {"x": "categorical", "y": "numeric", "z": "ordinal[2]"},
      "Change in value AND rank between two points in time.", "More than two time points (use bump/line).", _N),
    _vt("bullet", "Bullet chart", "ranking", "chart", {"x": "categorical", "y": "numeric", "target": "numeric"},
      "Actual vs target.", "No target value.", _R),
    _vt("ranked_columns", "Connected ranking columns", "ranking", "chart", {"x": "categorical", "measures": "numeric[2-5]"},
      "The same items (countries) ranked by 2–5 measures side by side, linked across columns (infographic style).",
      "Only one measure (use a bar chart).", _L, ("tooltip", "label_filter")),
    _vt("radar", "Radar / spider chart", "ranking", "chart", {"x": "categorical[3-8]", "y": "numeric", "z?": "categorical"},
      "Profile of 1–3 items across a few dimensions (spot patterns).", "Precise comparison; many items.", _R),
    _vt("parallel_coordinates", "Parallel coordinates", "ranking", "chart", {"measures": "numeric[3+]", "label": "any"},
      "Many items across many numeric dimensions.", "General audiences.", _R),
    _vt("circular_bar", "Circular bar chart", "ranking", "chart", {"x": "categorical", "y": "numeric"},
      "Eye-catching ranking of many items (decorative).", "Accurate comparison (inner bars look smaller).", _R),
    _vt("word_cloud", "Word cloud", "ranking", "chart", {"x": "text", "y": "numeric|count"},
      "Rough sense of the most frequent words.", "Anything that needs accuracy.", _R, ("tooltip",)),

    # Part of a whole (+ hierarchy)
    _vt("donut", "Donut chart", "part_of_whole", "chart", {"x": "categorical[2-6]", "y": "numeric"},
      "Share of a total with few parts (≤6); the centre shows the total.", "Many parts or similar sizes (use bars).", _L, legacy="donut"),
    _vt("pie", "Pie chart", "part_of_whole", "chart", {"x": "categorical[2-6]", "y": "numeric"},
      "Share of a total with 2–6 parts, labelled on the slices.", "Many parts or similar sizes (use bars).", _L),
    _vt("waffle", "Waffle / icon array", "part_of_whole", "chart", {"x": "categorical[2-5]", "y": "numeric"},
      "Proportions people can count (\"3 in 10\"); works with label icons.", "Many categories.", _N),
    _vt("stacked", "Stacked bar chart", "part_of_whole", "chart", {"x": "categorical", "z": "categorical", "y": "numeric"},
      "Totals AND their composition per category.", "Comparing the middle segments precisely.", _L, legacy="stacked"),
    _vt("stacked_100", "100% stacked bar", "part_of_whole", "chart", {"x": "categorical", "z": "categorical", "y": "numeric"},
      "Composition per category when totals differ a lot.", "The totals themselves matter.", _N),
    _vt("diverging_bar", "Diverging bar chart", "part_of_whole", "chart", {"x": "categorical", "z": "ordinal", "y": "numeric"},
      "Sentiment / Likert scales; positive vs negative.", "No natural middle.", _N),
    _vt("population_pyramid", "Population pyramid", "part_of_whole", "chart", {"x": "ordinal", "z": "categorical[2]", "y": "numeric"},
      "Two opposing groups across ordered bands (age × sex).", "More than two groups.", _N),
    _vt("waterfall", "Waterfall chart", "part_of_whole", "chart", {"x": "ordinal", "y": "numeric"},
      "How steps add and subtract to a total (budget, profit).", "Unordered categories.", _N),
    _vt("marimekko", "Marimekko chart", "part_of_whole", "chart", {"x": "categorical", "z": "categorical", "y": "numeric"},
      "Two categorical breakdowns where both widths and heights matter (market share).", "General audiences.", _R),
    _vt("treemap", "Treemap", "part_of_whole", "chart", {"x": "categorical", "y": "numeric", "parent?": "categorical"},
      "Many parts of a whole, optionally nested (budget by ministry > programme).", "Few parts (donut/bar is clearer).", _L,
      legacy="treemap"),
    _vt("sunburst", "Sunburst", "part_of_whole", "chart", {"levels": "categorical[2+]", "y": "numeric"},
      "Levels of a hierarchy and their shares.", "Deep trees with tiny slices.", _R),
    _vt("circle_packing", "Circular packing", "part_of_whole", "chart", {"levels": "categorical[1+]", "y": "numeric"},
      "Nested groups at a glance (decorative).", "Precise comparison.", _R),
    _vt("dendrogram", "Dendrogram", "part_of_whole", "chart", {"parent": "categorical", "label": "categorical"},
      "Structure of a tree / clustering result.", "No hierarchy in the data.", _R, ("tooltip",)),
    _vt("venn", "Venn diagram", "part_of_whole", "chart", {"sets": "binary[2-3]"},
      "Overlap between 2–3 sets.", "More than three sets.", _R, ("tooltip",)),

    # Evolution (data over time)
    _vt("line", "Line chart", "evolution", "chart", {"x": "temporal|ordinal", "y": "numeric", "z?": "categorical"},
      "Trend over time; focus on change. Several series with label filter.", "Few time points (use bars).", _L,
      ("tooltip", "label_filter", "measure_pick"), legacy="line"),
    _vt("area", "Area chart", "evolution", "chart", {"x": "temporal", "y": "numeric"},
      "Trend over time with emphasis on quantity.", "Several overlapping series.", _L,
      ("tooltip", "label_filter", "measure_pick"), legacy="area"),
    _vt("stacked_area", "Stacked area chart", "evolution", "chart", {"x": "temporal", "z": "categorical", "y": "numeric"},
      "Total over time and how its parts contribute.", "Reading individual series precisely.", _N),
    _vt("streamgraph", "Streamgraph", "evolution", "chart", {"x": "temporal", "z": "categorical", "y": "numeric"},
      "Many series' rise and fall over time (flow of attention).", "Exact values.", _R),
    _vt("bump", "Bump chart", "evolution", "chart", {"x": "temporal|ordinal", "z": "categorical", "y": "numeric"},
      "How rankings change over time (who overtook whom).", "Values matter more than ranks.", _N),
    _vt("bar_race", "Bar chart race", "evolution", "chart", {"x": "categorical", "y": "numeric", "time": "temporal"},
      "Ranking over time told as an animation (play button).", "Static reports.", _N, ("tooltip", "label_filter", "time_play")),
    _vt("gantt", "Gantt chart", "evolution", "chart", {"label": "categorical", "start": "temporal", "end": "temporal"},
      "Planning: tasks over time.", "Measures, not durations.", _R, ("tooltip",)),

    # Map (geospatial)
    _vt("extent_map", "Extent / location map", "map", "map", {"geometry|bbox": "any"},
      "Where the data is: its bounding box shown on a basemap with a margin. The default map and share image.",
      "When a measure should be shown (pick a thematic map).", _N, ("zoom",)),
    _vt("point_map", "Point map", "map", "map", {"geometry": "point", "label?": "any"},
      "Where individual things are (stations, incidents, facilities).", "Thousands of overlapping points (use heat/hexbin).", _L,
      ("tooltip", "label_filter", "zoom", "time_play"), legacy="map"),
    _vt("bubble_map", "Proportional symbol (bubble) map", "map", "map", {"geometry": "point|polygon", "y": "numeric"},
      "Counts or totals per place (population, cases): size = amount.", "Rates or percentages (use choropleth).", _L,
      ("tooltip", "label_filter", "zoom", "measure_pick", "time_play")),
    _vt("choropleth", "Choropleth map", "map", "map", {"geometry": "polygon", "y": "numeric"},
      "Rates, percentages or densities by region (unemployment %, per-capita).", "Raw totals (big regions dominate).", _L,
      ("tooltip", "label_filter", "zoom", "measure_pick", "time_play"), legacy="map"),
    _vt("geo_heatmap", "Geographic heatmap (density)", "map", "map", {"geometry": "point", "y?": "numeric"},
      "Hotspots without region borders (crime, pickups, sightings).", "Few points, or exact locations matter.", _L,
      ("tooltip", "zoom", "time_play"), legacy="map_heat"),
    _vt("line_map", "Line / route map", "map", "map", {"geometry": "line", "y?": "numeric"},
      "Rivers, roads, routes, tracks; width or colour = a measure.", "Point or area data.", _L,
      ("tooltip", "label_filter", "zoom"), legacy="map"),
    _vt("connection_map", "Connection / flow map", "map", "map", {"source": "point", "target": "point", "y?": "numeric"},
      "Origin–destination movement (migration, trade, flights).", "No pairs of places.", _N,
      ("tooltip", "label_filter", "zoom", "time_play")),
    _vt("hexbin_map", "Hexbin map", "map", "map", {"geometry": "point", "y?": "numeric"},
      "Even-sized cells summarise dense points without region-size bias.", "Few points.", _R, ("tooltip", "zoom")),
    _vt("tile_map", "Tile map (equal-size regions)", "map", "map", {"region_code": "categorical", "y": "numeric"},
      "Every region gets equal weight (US states, EU countries).", "Real shapes/areas matter.", _R, ("tooltip", "label_filter")),
    _vt("cartogram", "Cartogram", "map", "map", {"geometry": "polygon", "y": "numeric"},
      "Region area distorted to the value (population-weighted world).", "Readers need real geography.", _R, ("tooltip",)),
    _vt("dot_density_map", "Dot density map", "map", "map", {"geometry": "polygon", "y": "count"},
      "Show distribution and mix of a count inside regions.", "Small numbers.", _R, ("tooltip", "zoom")),

    # Flow & relationship
    _vt("sankey", "Sankey diagram", "flow", "chart", {"source": "categorical", "target": "categorical", "y": "numeric"},
      "Quantities splitting and flowing between stages (energy, budget, users).", "No quantities on the links.", _R),
    _vt("funnel", "Funnel chart", "flow", "chart", {"x": "ordinal", "y": "numeric"},
      "Values decreasing step by step (conversion).", "Steps that are not sequential.", _N),
    _vt("chord", "Chord diagram", "flow", "chart", {"source": "categorical", "target": "categorical", "y": "numeric"},
      "Two-way flows between a few groups (trade between regions).", "More than ~10 groups.", _R),
    _vt("arc_diagram", "Arc diagram", "flow", "chart", {"source": "categorical", "target": "categorical"},
      "Connections between items placed on one line.", "Dense graphs.", _R),
    _vt("network", "Network diagram", "flow", "chart", {"source": "any", "target": "any", "y?": "numeric"},
      "Who connects to whom; clusters and hubs.", "Hairballs of thousands of links.", _R, ("tooltip", "zoom")),
    _vt("edge_bundling", "Hierarchical edge bundling", "flow", "chart", {"source": "any", "target": "any", "parent": "categorical"},
      "Connections between members of a hierarchy.", "General audiences.", _R),
]

# Every type with a renderer (app/services/viz_recommender.VIZ_REGISTRY + the frontend) is live; the rest stay planned.
from app.services.viz_recommender import VIZ_REGISTRY as _RENDERERS  # noqa: E402
for _t in SEED_VISUAL_TYPES:
    if (_t["legacy_code"] or _t["code"]) in _RENDERERS:
        _t["status"] = _L

# Layout options that apply to many types (not types of their own)

# ── simple AI selection prompt (filled from the dataset profile; answers only with enabled codes) ──
SEED_SELECTION_PROMPT = """You choose the best visual for a dataset on a data-journalism site.
Goal of the post (from the publisher, may be empty): {problem_statement}
Dataset: "{title}" — {row_count} rows; geometry: {geometry_type}; bounding box: {bbox}.
Columns (name: kind, distinct values): {columns}

Allowed visual types (code — category — needs — best when):
{allowed_types}

Rules:
1. First decide WHAT the post must show: distribution, correlation, ranking/comparison, part of a whole,
   evolution over time, map (where), or flow/relationship. Prefer "map" when there is geometry and the
   question is about place; prefer "evolution" when there is a time column and the question is about change.
2. Pick the simplest allowed type that fits that goal AND whose needs the columns satisfy.
   Few categories → bar/column; many or long names → horizontal bar; shares of ≤6 parts → donut;
   rates by region → choropleth; totals at places → bubble map; dense points → geographic heatmap.
3. One chart, one message. Never pick a type because it looks impressive.
4. Choose the label column (used as the dropdown filter) and, if present, the time column (play button).
Answer with JSON only:
{{"category": "...", "visual_type": "<code>", "x": "<col|null>", "y": "<col|count>", "z": "<col|null>",
  "size": "<col|null>", "label": "<col|null>", "time": "<col|null>", "reason": "<one sentence>"}}"""



# ── misc DB: AI prompt templates ──────────────────────────────────────────────
SEED_AI_PROMPTS = [
    {"code": "visual_selection", "template": SEED_SELECTION_PROMPT,
     "description": "Picks the visual type, columns, label filter and time column for a dataset. "
                    "Placeholders: problem_statement, title, row_count, geometry_type, bbox, columns, allowed_types."},
]


def _run_seed(include_dev_admin: bool) -> dict:
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert
    from app.db.database import MiscSessionLocal
    from app.db.models import AiPromptRecord, RegionRecord, VisualCategoryRecord, VisualTypeRecord
    out = {}

    # roles (users DB)
    with UsersSessionLocal() as db:
        added = 0
        for r in SEED_ROLES:
            if not db.query(RoleRecord).filter(RoleRecord.name == r["name"]).first():
                db.add(RoleRecord(name=r["name"], permissions=r["permissions"]))
                added += 1
        db.commit()
        out["roles_added"] = added

    # post categories (posts DB): add missing; fill description/colour only where empty
    with PostsSessionLocal() as db:
        added = 0
        for i, c in enumerate(SEED_POST_CATEGORIES):
            row = db.query(CategoryRecord).filter(CategoryRecord.slug == c["slug"]).first()
            if row is None:
                db.add(CategoryRecord(slug=c["slug"], label=c["label"], description=c["description"],
                                      theme_color=c["theme_color"], sort_order=i + 1))
                added += 1
            else:
                row.description = row.description or c["description"]
                row.theme_color = row.theme_color or c["theme_color"]
                row.sort_order = row.sort_order or i + 1
        db.commit()
        out["post_categories_added"] = added

    with MiscSessionLocal() as db:
        # regions: the 8 top-level regions + every country under its continent. Upserted, so re-running /seed
        # repairs names / parents / boxes; anything else (old UN sub-regions, "americas", ad-hoc test rows) is
        # switched off but kept, so nothing that references it breaks.
        top = [{"code": c, "name": n, "kind": k, "parent_code": p, "m49": m, "bbox": bb, "iso_a2": None, "iso_a3": None,
                "sort_order": i, "is_active": 1} for i, (c, n, k, p, m, bb) in enumerate(SEED_REGIONS)]
        countries = [{"code": c, "name": n, "kind": "country", "parent_code": p, "m49": m, "bbox": bb,
                      "iso_a2": c.upper(), "iso_a3": a3, "sort_order": 100 + i, "is_active": 1}
                     for i, (c, n, p, a3, m, bb) in enumerate(SEED_COUNTRIES)]
        existing = {r[0] for r in db.query(RegionRecord.code).all()}
        st = insert(RegionRecord).values(top + countries)
        db.execute(st.on_conflict_do_update(index_elements=["code"], set_={
            k: st.excluded[k] for k in ("name", "kind", "parent_code", "m49", "bbox", "iso_a2", "iso_a3", "sort_order", "is_active")}))
        out["regions_added"] = len({r["code"] for r in top + countries} - existing)
        out["countries"] = len(countries)
        keep = [r[0] for r in SEED_REGIONS] + [r[0] for r in SEED_COUNTRIES]
        out["regions_deactivated"] = db.execute(
            update(RegionRecord).where(RegionRecord.code.notin_(keep),
                                       RegionRecord.is_active == 1).values(is_active=0)).rowcount or 0
        # visual categories + types: refreshed from here; `enabled` only set when a type is first added
        cats = [{"code": c["code"], "name": c["name"], "color": c["color"], "question": c["question"],
                 "guide_goals": c["guide_goals"], "sort_order": c["sort"]} for c in SEED_VISUAL_CATEGORIES]
        st = insert(VisualCategoryRecord).values(cats)
        db.execute(st.on_conflict_do_update(index_elements=["code"], set_={
            k: st.excluded[k] for k in ("name", "color", "question", "guide_goals", "sort_order")}))
        types = [{"code": t["code"], "name": t["name"], "category": t["category"], "kind": t["kind"],
                  "roles": t["roles"], "best_when": t["best_when"], "avoid_when": t["avoid_when"],
                  "status": t["status"], "interactions": t["interactions"], "legacy_code": t["legacy_code"],
                  "enabled": 1 if t["status"] == "live" else 0, "sort_order": i + 1}
                 for i, t in enumerate(SEED_VISUAL_TYPES)]
        st = insert(VisualTypeRecord).values(types)
        db.execute(st.on_conflict_do_update(index_elements=["code"], set_={
            k: st.excluded[k] for k in ("name", "category", "kind", "roles", "best_when", "avoid_when",
                                        "status", "interactions", "legacy_code", "sort_order")}))
        out["visual_catalog"] = {"categories": len(cats), "types": len(types)}
        # AI prompts: add missing only (an edited prompt is kept)
        out["ai_prompts_added"] = db.execute(
            insert(AiPromptRecord).values(SEED_AI_PROMPTS).on_conflict_do_nothing(index_elements=["code"])).rowcount or 0
        db.commit()

    # development admin (users DB)
    if include_dev_admin:
        a = SEED_DEV_ADMIN
        with UsersSessionLocal() as db:
            exists = db.query(PlatformUserRecord).filter(
                (PlatformUserRecord.user_handle == a["user_handle"]) |
                (PlatformUserRecord.email_address == a["email_address"])).first()
            out["dev_admin_created"] = not exists
            if not exists:
                role = db.query(RoleRecord).filter(RoleRecord.name == a["role"]).first()
                user = PlatformUserRecord(
                    user_id=uuid.uuid4(), user_handle=a["user_handle"], email_address=a["email_address"],
                    hashed_security_password=auth_service.get_password_hash(a["password"]), title=a["title"],
                    role_id=role.role_id if role else None, is_verified=1, is_active=1)
                db.add(user)
                db.commit()
                loc = a["location"]
                db.add(UserLocationRecord(user_id=user.user_id, place=loc["place"], state=loc["state"],
                                          country=loc["country"],
                                          formatted_string=", ".join([loc["place"], loc["state"], loc["country"]])))
                db.commit()
    return out


@app.get("/seed")
def seed_database(x_seed_token: Optional[str] = Header(None)):
    import secrets
    dev = os.getenv("APP_ENV", "development") == "development"
    if not dev:
        token = os.getenv("SEED_TOKEN", "")
        if not token or not x_seed_token or not secrets.compare_digest(x_seed_token, token):
            raise HTTPException(status_code=404, detail="Not Found")
    try:
        return {"status": "Success", **_run_seed(include_dev_admin=dev)}
    except Exception as e:
        return {"status": "Error", "details": f"{type(e).__name__}: {e}"}