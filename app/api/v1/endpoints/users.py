import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_users_db
from app.db.models import PlatformUserRecord, RoleRecord, UserLocationRecord
from app.services.auth_service import get_current_authenticated_user, RoleChecker
from app.schemas.user_schema import UserProfileData, UserSettingsUpdatePayload

router = APIRouter()

@router.get("/me")
def get_my_profile(current_user: PlatformUserRecord = Depends(get_current_authenticated_user)):
    """Returns the hydrated profile data for the active authenticated session."""
    return {
        "user_id": str(current_user.user_id),
        "user_handle": current_user.user_handle,
        "email_address": current_user.email_address,
        "avatar_path": current_user.avatar_path,
        "title": current_user.title,
        "follower_count": current_user.follower_count,
        "following_count": current_user.following_count,
        "role_name": current_user.role.name if current_user.role else "viewer"
    }

@router.get("/profile/{handle}")
def get_user_profile(handle: str, db: Session = Depends(get_users_db)):
    """Fetch public profile stats and metadata for a specific user handle."""
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "user_id": str(user.user_id),
        "user_handle": user.user_handle,
        "title": user.title,
        "avatar_path": user.avatar_path,
        "follower_count": user.follower_count,
        "following_count": user.following_count,
        "post_count": user.post_count,
        "linkedin_url": getattr(user, 'linkedin_url', None),
        "website_url": getattr(user, 'website_url', None),
        "joined_at": user.created_timestamp
    }

@router.put("/settings", response_model=UserProfileData)
def update_user_settings(
    payload: UserSettingsUpdatePayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    # Update base fields
    if payload.title is not None:
        current_user.title = payload.title
    if payload.linkedin_url is not None:
        current_user.linkedin_url = payload.linkedin_url
    if payload.medium_url is not None:
        current_user.medium_url = payload.medium_url
    if payload.website_url is not None:
        current_user.website_url = payload.website_url

    # Handle Location
    if any([payload.place, payload.state, payload.country]):
        if not current_user.location:
            current_user.location = UserLocationRecord(user_id=current_user.user_id)
            db.add(current_user.location)
            db.flush()

        if payload.place is not None:
            current_user.location.place = payload.place
        if payload.state is not None:
            current_user.location.state = payload.state
        if payload.country is not None:
            current_user.location.country = payload.country
            
        parts = [p for p in [current_user.location.place, current_user.location.state, current_user.location.country] if p]
        current_user.location.formatted_string = ", ".join(parts)

    db.commit()
    db.refresh(current_user)
    return current_user

@router.put("/{user_id}/status")
def set_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    db: Session = Depends(get_users_db),
    # Only admins or support staff can deactivate users
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "support"]))
):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_active = is_active
    db.commit()
    return {"message": f"User status set to {'active' if is_active else 'deactivated'}"}