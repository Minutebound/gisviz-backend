import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.database import get_users_db
from app.db.models import PlatformUserRecord, RoleRecord, UserLocationRecord, FollowCurrentRecord
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
def get_user_profile(
    handle: str, 
    current_user_id: str = Query(None),
    db: Session = Depends(get_users_db)
):
    """Fetch public profile stats and metadata for a specific user handle."""
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == handle).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if the current user is following this profile
    is_following = False
    if current_user_id:
        try:
            uuid_obj = uuid.UUID(current_user_id)
            follow_record = db.query(FollowCurrentRecord).filter(
                FollowCurrentRecord.actor_user_id == uuid_obj,
                FollowCurrentRecord.target_user_id == user.user_id
            ).first()
            if follow_record:
                is_following = True
        except ValueError:
            pass # Fail silently on invalid UUID string, default to false
            
    # Extract Location
    location_str = None
    if user.location and user.location.formatted_string:
        location_str = user.location.formatted_string

    return {
        "user_id": str(user.user_id),
        "user_handle": user.user_handle,
        "title": user.title,
        "location": location_str,
        "avatar_path": user.avatar_path,
        "follower_count": user.follower_count,
        "following_count": user.following_count,
        "post_count": user.post_count,
        "linkedin_url": getattr(user, 'linkedin_url', None),
        "website_url": getattr(user, 'website_url', None),
        "joined_at": user.created_timestamp,
        "is_following": is_following
    }

@router.get("/popular")
def get_popular_publishers(
    limit: int = Query(50, ge=1, le=100),
    current_user_id: str = Query(None),
    db: Session = Depends(get_users_db)
):
    """Fetch popular publishers (users with post_count > 0) based on follower count."""
    users = (
        db.query(PlatformUserRecord)
        .filter(PlatformUserRecord.post_count > 0)
        .order_by(PlatformUserRecord.follower_count.desc())
        .limit(limit)
        .all()
    )
    
    results = []
    for u in users:
        is_followed = False
        if current_user_id:
            try:
                uuid_obj = uuid.UUID(current_user_id)
                follow_record = db.query(FollowCurrentRecord).filter(
                    FollowCurrentRecord.actor_user_id == uuid_obj,
                    FollowCurrentRecord.target_user_id == u.user_id
                ).first()
                if follow_record:
                    is_followed = True
            except ValueError:
                pass
                
        results.append({
            "user_id": str(u.user_id),
            "user_handle": u.user_handle,
            "avatar_path": u.avatar_path,
            "follower_count": u.follower_count,
            "is_followed": is_followed
        })
        
    return results

@router.put("/settings", response_model=UserProfileData)
def update_user_settings(
    payload: UserSettingsUpdatePayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    if payload.title is not None: current_user.title = payload.title
    if payload.linkedin_url is not None: current_user.linkedin_url = payload.linkedin_url
    if payload.medium_url is not None: current_user.medium_url = payload.medium_url
    if payload.website_url is not None: current_user.website_url = payload.website_url

    if any([payload.place, payload.state, payload.country]):
        if not current_user.location:
            current_user.location = UserLocationRecord(user_id=current_user.user_id)
            db.add(current_user.location)
            db.flush()

        if payload.place is not None: current_user.location.place = payload.place
        if payload.state is not None: current_user.location.state = payload.state
        if payload.country is not None: current_user.location.country = payload.country
            
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
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "support"]))
):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_active = is_active
    db.commit()
    return {"message": f"User status set to {'active' if is_active else 'deactivated'}"}