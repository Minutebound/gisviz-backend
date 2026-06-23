from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_users_db
from app.db.models import PlatformUserRecord
from app.schemas.user_schema import FollowEventData
from app.services.follow_service import follow_service
from app.services.auth_service import get_current_authenticated_user

router = APIRouter()


@router.post("/{target_id}/follow")
def follow_user(
    target_id: str,
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return follow_service.follow(users_db, str(current_user.user_id), target_id)


@router.post("/{target_id}/unfollow")
def unfollow_user(
    target_id: str,
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return follow_service.unfollow(users_db, str(current_user.user_id), target_id)


@router.get("/{target_id}/followers")
def list_followers(target_id: str, users_db: Session = Depends(get_users_db)):
    rows = follow_service.followers_of(users_db, target_id)
    return [
        {"actor_user_id": str(r.actor_user_id), "followed_since": r.followed_since}
        for r in rows
    ]


@router.get("/{target_id}/history", response_model=List[FollowEventData])
def follow_history(
    target_id: str,
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return follow_service.follow_history(users_db, str(current_user.user_id), target_id)