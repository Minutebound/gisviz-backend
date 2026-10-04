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
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS theme_color VARCHAR(7)",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0",
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
    ]
    dataset_stmts = [
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS columns JSONB",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS data_uploaded_at TIMESTAMPTZ",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS region VARCHAR(60)",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT",
        "ALTER TABLE datasets ADD COLUMN IF NOT EXISTS show_data INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_datasets_region ON datasets (region)",
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
app.include_router(datasets_endpoints.router, prefix=f"{settings.API_V0_STR}/datasets",  tags=["Datasets"])
app.include_router(visuals_endpoints.router, prefix=f"{settings.API_V0_STR}/visuals",    tags=["Visuals"])
app.include_router(regions_endpoints.router, prefix=f"{settings.API_V0_STR}/regions",    tags=["Regions"])
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
    ("global", "Global", "global", None, "001", [-180, -90, 180, 90]),
    ("africa", "Africa", "continent", "global", "002", [-25.4, -34.9, 63.5, 37.6]),
    ("americas", "Americas", "continent", "global", "019", [-170, -56, -30, 83.7]),
    ("asia", "Asia", "continent", "global", "142", [25, -11, 180, 82]),
    ("europe", "Europe", "continent", "global", "150", [-31.3, 34.5, 69, 81.9]),
    ("oceania", "Oceania", "continent", "global", "009", [110, -53, 180, 0]),
    ("antarctica", "Antarctica", "continent", "global", "010", [-180, -90, 180, -60]),
    ("northern-africa", "Northern Africa", "subregion", "africa", "015", None),
    ("eastern-africa", "Eastern Africa", "subregion", "africa", "014", None),
    ("middle-africa", "Middle Africa", "subregion", "africa", "017", None),
    ("southern-africa", "Southern Africa", "subregion", "africa", "018", None),
    ("western-africa", "Western Africa", "subregion", "africa", "011", None),
    ("caribbean", "Caribbean", "subregion", "americas", "029", None),
    ("central-america", "Central America", "subregion", "americas", "013", None),
    ("south-america", "South America", "subregion", "americas", "005", None),
    ("northern-america", "Northern America", "subregion", "americas", "021", None),
    ("central-asia", "Central Asia", "subregion", "asia", "143", None),
    ("eastern-asia", "Eastern Asia", "subregion", "asia", "030", None),
    ("south-eastern-asia", "South-eastern Asia", "subregion", "asia", "035", None),
    ("southern-asia", "Southern Asia", "subregion", "asia", "034", None),
    ("western-asia", "Western Asia", "subregion", "asia", "145", None),
    ("eastern-europe", "Eastern Europe", "subregion", "europe", "151", None),
    ("northern-europe", "Northern Europe", "subregion", "europe", "154", None),
    ("southern-europe", "Southern Europe", "subregion", "europe", "039", None),
    ("western-europe", "Western Europe", "subregion", "europe", "155", None),
    ("australia-new-zealand", "Australia and New Zealand", "subregion", "oceania", "053", None),
    ("melanesia", "Melanesia", "subregion", "oceania", "054", None),
    ("micronesia", "Micronesia", "subregion", "oceania", "057", None),
    ("polynesia", "Polynesia", "subregion", "oceania", "061", None),
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
    _vt("radar", "Radar / spider chart", "ranking", "chart", {"x": "categorical[3-8]", "y": "numeric", "z?": "categorical"},
      "Profile of 1–3 items across a few dimensions (spot patterns).", "Precise comparison; many items.", _R),
    _vt("parallel_coordinates", "Parallel coordinates", "ranking", "chart", {"measures": "numeric[3+]", "label": "any"},
      "Many items across many numeric dimensions.", "General audiences.", _R),
    _vt("circular_bar", "Circular bar chart", "ranking", "chart", {"x": "categorical", "y": "numeric"},
      "Eye-catching ranking of many items (decorative).", "Accurate comparison (inner bars look smaller).", _R),
    _vt("word_cloud", "Word cloud", "ranking", "chart", {"x": "text", "y": "numeric|count"},
      "Rough sense of the most frequent words.", "Anything that needs accuracy.", _R, ("tooltip",)),

    # Part of a whole (+ hierarchy)
    _vt("donut", "Donut / pie chart", "part_of_whole", "chart", {"x": "categorical[2-6]", "y": "numeric"},
      "Share of a total with few parts (≤6).", "Many parts or similar sizes (use bars).", _L, legacy="donut"),
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
      ("tooltip", "label_filter", "zoom", "measure_pick", "time_play"), legacy="map"),
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
        # regions: add missing only
        rows = [{"code": c, "name": n, "kind": k, "parent_code": p, "m49": m, "bbox": bb, "sort_order": i, "is_active": 1}
                for i, (c, n, k, p, m, bb) in enumerate(SEED_REGIONS)]
        out["regions_added"] = db.execute(
            insert(RegionRecord).values(rows).on_conflict_do_nothing(index_elements=["code"])).rowcount or 0
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