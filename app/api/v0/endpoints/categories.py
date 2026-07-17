"""
app/api/v0/endpoints/categories.py
====================================
All category routes.

Redis key layout (all under the "gisviz-cache:" FastAPICache prefix):
  gisviz-cache:categories:list              — full sorted list (24 h TTL)
  gisviz-cache:categories:trending          — top-N by usage_count  (1 h TTL)

Mutation rule:
  Every write (create / update / delete / approve) must call BOTH
  _invalidate_category_list() and _invalidate_trending_categories()
  so the Sidebar and the control page always see fresh data.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_cache import FastAPICache
from fastapi_cache.decorator import cache
from sqlalchemy.orm import Session
from pydantic import BaseModel
import uuid
from datetime import datetime, timezone

from app.db.database import get_posts_db
from app.db.models import (
    CategoryRecord,
    PendingCategoryRecord,
    PlatformUserRecord,
    PostCategoryLink,
)
from app.schemas.post_schema import CategoryData, PendingCategoryData, PendingCategorySuggestion
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()


# ── Request schemas ───────────────────────────────────────────────────────────

class CategoryUpdatePayload(BaseModel):
    label: str
    slug: str


# ── Redis key constants ───────────────────────────────────────────────────────

# Both keys live inside the "gisviz-cache:" namespace that FastAPICache
# prefixes automatically, so what Redis actually stores is exactly these strings.
_CATEGORY_LIST_KEY     = "gisviz-cache:categories:list"
_CATEGORY_TRENDING_KEY = "gisviz-cache:categories:trending"

_TRENDING_TTL  = 3600       # 1 hour  — refreshes often enough to reflect new posts
_LIST_TTL      = 86400      # 24 hours


# ── Cache invalidation helpers ────────────────────────────────────────────────

def _invalidate_all_category_caches() -> None:
    """
    Bust ALL category Redis keys after any mutation (create/update/delete/approve).

    Uses CacheService (sync redis-py) which matches how the endpoints
    now write cache entries — no async FastAPICache.get_backend() needed.

    Keys cleared:
      gisviz-cache:categories:list          — full alphabetical list
      gisviz-cache:categories:trending:*    — per-limit trending keys
    """
    from app.services.cache_service import cache_service

    # Delete the list key
    cache_service.delete(_CATEGORY_LIST_KEY)

    # Delete all trending variants (different limit values)
    # CacheService.delete_pattern uses SCAN — safe on large keyspaces
    cache_service.delete_pattern(f"{_CATEGORY_TRENDING_KEY}:*")
    print("[cache] category caches invalidated")


# ── GET / — full list (alphabetical) ─────────────────────────────────────────

@router.get("/", response_model=List[CategoryData])
async def get_categories(db: Session = Depends(get_posts_db)):
    """
    Public. Returns every category sorted alphabetically.

    Manual Redis cache via CacheService (not @cache decorator) so we can:
      - Skip caching when the DB returns empty (self-heals in 30 s)
      - Use a long TTL (24 h) once data exists
      - Bust reliably via _invalidate_all_category_caches() on mutations
    """
    from app.services.cache_service import cache_service

    cached_val = cache_service.get(_CATEGORY_LIST_KEY)
    if cached_val is not None:
        return cached_val

    rows = db.query(CategoryRecord).order_by(CategoryRecord.label).all()
    result = [
        {"category_id": r.category_id, "slug": r.slug, "label": r.label, "usage_count": r.usage_count}
        for r in rows
    ]
    ttl = _LIST_TTL if result else 30
    cache_service.set(_CATEGORY_LIST_KEY, result, ttl_seconds=ttl)
    return rows


# ── GET /trending — top-N by usage_count ─────────────────────────────────────

@router.get("/trending", response_model=List[CategoryData])
async def get_trending_categories(
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_posts_db),
):
    """
    Public. Returns the top-N categories ordered by usage_count descending.

    Manual Redis cache via CacheService so empty results are NOT cached
    for a full hour — they self-heal in 30 s as soon as posts are tagged.
    """
    from app.services.cache_service import cache_service

    # Key includes limit so different sidebar sizes get their own entry
    key = f"{_CATEGORY_TRENDING_KEY}:{limit}"

    cached_val = cache_service.get(key)
    if cached_val is not None:
        return cached_val

    rows = (
        db.query(CategoryRecord)
        .filter(CategoryRecord.usage_count > 0)
        .order_by(CategoryRecord.usage_count.desc())
        .limit(limit)
        .all()
    )
    result = [
        {"category_id": r.category_id, "slug": r.slug, "label": r.label, "usage_count": r.usage_count}
        for r in rows
    ]
    ttl = _TRENDING_TTL if result else 30
    cache_service.set(key, result, ttl_seconds=ttl)
    return rows


# ── POST /suggest — authenticated user proposes a new category ────────────────

@router.post("/suggest", response_model=PendingCategoryData)
def suggest_category(
    payload: PendingCategorySuggestion,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    normalized = payload.label.strip().lower().replace(" ", "-")
    if db.query(CategoryRecord).filter(CategoryRecord.slug == normalized).first():
        raise HTTPException(status_code=400, detail="Category already exists")
    already_pending = db.query(PendingCategoryRecord).filter(
        PendingCategoryRecord.normalized_slug == normalized,
        PendingCategoryRecord.status == "pending",
    ).first()
    if already_pending:
        raise HTTPException(status_code=409, detail="This category is already pending review")
    pending = PendingCategoryRecord(
        label=payload.label.strip(),
        normalized_slug=normalized,
        suggested_by_user_id=current_user.user_id,
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)
    return pending


# ── GET /pending — list pending suggestions ───────────────────────────────────

@router.get("/pending", response_model=List[PendingCategoryData])
def get_pending_categories(
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    return (
        db.query(PendingCategoryRecord)
        .filter(PendingCategoryRecord.status == "pending")
        .order_by(PendingCategoryRecord.created_timestamp.asc())
        .all()
    )


# ── POST /pending/{id}/approve ────────────────────────────────────────────────

@router.post("/pending/{pending_id}/approve")
async def approve_pending_category(
    pending_id: uuid.UUID,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    pending = db.query(PendingCategoryRecord).filter(
        PendingCategoryRecord.pending_id == pending_id
    ).first()
    if not pending or pending.status != "pending":
        raise HTTPException(status_code=404, detail="Pending category not found or already processed")

    if db.query(CategoryRecord).filter(CategoryRecord.slug == pending.normalized_slug).first():
        pending.status = "rejected"
        pending.reviewed_timestamp = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=409, detail="A category with this slug already exists")

    new_cat = CategoryRecord(slug=pending.normalized_slug, label=pending.label)
    db.add(new_cat)
    pending.status = "approved"
    pending.reviewed_timestamp = datetime.now(timezone.utc)
    db.commit()
    db.refresh(new_cat)

    # Bust both cache keys so the list and the trending sidebar update immediately
    _invalidate_all_category_caches()

    return {
        "status": "approved",
        "category": {
            "category_id": new_cat.category_id,
            "slug": new_cat.slug,
            "label": new_cat.label,
        },
    }


# ── POST /pending/{id}/reject ─────────────────────────────────────────────────

@router.post("/pending/{pending_id}/reject")
def reject_pending_category(
    pending_id: uuid.UUID,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    pending = db.query(PendingCategoryRecord).filter(
        PendingCategoryRecord.pending_id == pending_id
    ).first()
    if not pending or pending.status != "pending":
        raise HTTPException(status_code=404, detail="Pending category not found or already processed")
    pending.status = "rejected"
    pending.reviewed_timestamp = datetime.now(timezone.utc)
    db.commit()
    return {"status": "rejected", "label": pending.label}


# ── POST / — admin create a category directly ─────────────────────────────────

@router.post("/", response_model=CategoryData)
async def create_category(
    payload: CategoryUpdatePayload,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    slug = payload.slug.strip().lower().replace(" ", "-")
    if db.query(CategoryRecord).filter(CategoryRecord.slug == slug).first():
        raise HTTPException(status_code=409, detail="Category with this slug already exists")
    cat = CategoryRecord(slug=slug, label=payload.label.strip())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    _invalidate_all_category_caches()
    return cat


# ── PUT /{category_id} — admin rename ────────────────────────────────────────

@router.put("/{category_id}", response_model=CategoryData)
async def update_category(
    category_id: int,
    payload: CategoryUpdatePayload,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    cat = db.query(CategoryRecord).filter(CategoryRecord.category_id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    cat.label = payload.label.strip()
    cat.slug  = payload.slug.strip().lower().replace(" ", "-")
    db.commit()
    db.refresh(cat)
    _invalidate_all_category_caches()
    return cat


# ── DELETE /{category_id} — admin only ───────────────────────────────────────

@router.delete("/{category_id}")
async def delete_category(
    category_id: int,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    cat = db.query(CategoryRecord).filter(
        CategoryRecord.category_id == category_id
    ).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    label = cat.label  # capture before deletion

    # Remove all post→category links first (atomic with the category delete below)
    db.query(PostCategoryLink).filter(
        PostCategoryLink.category_id == category_id
    ).delete(synchronize_session=False)

    db.delete(cat)
    db.commit()

    _invalidate_all_category_caches()
    return {"status": "deleted", "label": label}