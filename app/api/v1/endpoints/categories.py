from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import uuid
from datetime import datetime, timezone

from app.db.database import get_posts_db
from app.db.models import CategoryRecord, PendingCategoryRecord, PlatformUserRecord
from app.schemas.post_schema import CategoryData, PendingCategoryData, PendingCategorySuggestion
from app.services.auth_service import get_current_authenticated_user, RoleChecker

router = APIRouter()


@router.get("/", response_model=List[CategoryData])
def get_categories(db: Session = Depends(get_posts_db)):
    return db.query(CategoryRecord).order_by(CategoryRecord.label).all()


@router.post("/suggest", response_model=PendingCategoryData)
def suggest_category(
    payload: PendingCategorySuggestion,
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    """Any authenticated user can suggest a category."""
    normalized = payload.label.strip().lower().replace(" ", "-")

    if db.query(CategoryRecord).filter(CategoryRecord.slug == normalized).first():
        raise HTTPException(status_code=400, detail="Category already exists")

    # Check if already pending
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


@router.get("/pending", response_model=List[PendingCategoryData])
def get_pending_categories(
    db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor"])),
):
    """Admin and editor only — viewers/publishers should not see the pending queue."""
    return (
        db.query(PendingCategoryRecord)
        .filter(PendingCategoryRecord.status == "pending")
        .order_by(PendingCategoryRecord.created_timestamp.asc())
        .all()
    )


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

    # Check slug not already taken (race condition guard)
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