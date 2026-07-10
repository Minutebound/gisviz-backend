from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
import uuid
from datetime import datetime, timezone

from app.db.database import get_posts_db
from app.db.models import CategoryRecord, PendingCategoryRecord, PlatformUserRecord, PostCategoryLink
from app.schemas.post_schema import CategoryData, PendingCategoryData, PendingCategorySuggestion
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()


class CategoryUpdatePayload(BaseModel):
    label: str
    slug: str


# ── GET / ────────────────────────────────────────────────────────────

@router.get("/", response_model=List[CategoryData])
def get_categories(db: Session = Depends(get_posts_db)):
    return db.query(CategoryRecord).order_by(CategoryRecord.label).all()


# ── POST /suggest ────────────────────────────────────────────────────

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


# ── GET /pending ─────────────────────────────────────────────────────

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


# ── POST /pending/{id}/approve ───────────────────────────────────────

@router.post("/pending/{pending_id}/approve")
def approve_pending_category(
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
    return {"status": "approved", "category": {"category_id": new_cat.category_id, "slug": new_cat.slug, "label": new_cat.label}}


# ── POST /pending/{id}/reject ────────────────────────────────────────

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


# ── POST / — admin create category directly ──────────────────────────

@router.post("/", response_model=CategoryData)
def create_category(
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
    return cat


# ── PUT /{category_id} — admin rename ───────────────────────────────

@router.put("/{category_id}", response_model=CategoryData)
def update_category(
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
    return cat


# ── DELETE /{category_id} — admin only ──────────────────────────────

@router.delete("/{category_id}")
def delete_category(
    category_id: int,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    cat = db.query(CategoryRecord).filter(
        CategoryRecord.category_id == category_id
    ).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
 
    label = cat.label   # capture before delete
 
    # 1. Explicitly remove all post→category links for this category.
    #    synchronize_session=False skips updating the in-session identity map —
    #    safe here because we're deleting the parent immediately after.
    db.query(PostCategoryLink).filter(
        PostCategoryLink.category_id == category_id
    ).delete(synchronize_session=False)
 
    # 2. Now delete the category row itself.
    db.delete(cat)
 
    # 3. Single commit — both operations land atomically.
    db.commit()
 
    return {"status": "deleted", "label": label}
