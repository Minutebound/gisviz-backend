from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_posts_db
from app.db.models import PlatformUserRecord
from app.schemas.post_schema import (
    CategoryData,
    PendingTagSuggestion,
    PendingTagData,
)
from app.services.category_service import category_service
from app.services.auth_service import get_current_authenticated_user

router = APIRouter()


@router.get("", response_model=List[CategoryData])
def list_categories(posts_db: Session = Depends(get_posts_db)):
    return category_service.list_categories(posts_db)


@router.post("/suggest", response_model=PendingTagData, status_code=201)
def suggest_tag(
    payload: PendingTagSuggestion,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return category_service.suggest_tag(posts_db, payload.label, str(current_user.user_id))


# ----- Admin / moderation (gate these behind a role check in production) -----
@router.get("/pending", response_model=List[PendingTagData])
def list_pending(
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return category_service.list_pending(posts_db)


@router.post("/pending/{pending_id}/approve", response_model=CategoryData)
def approve_tag(
    pending_id: str,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return category_service.approve_tag(posts_db, pending_id)


@router.post("/pending/{pending_id}/reject", status_code=204)
def reject_tag(
    pending_id: str,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    category_service.reject_tag(posts_db, pending_id)
    return None