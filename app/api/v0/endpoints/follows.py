import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_users_db
from app.db.models import PlatformUserRecord, FollowCurrentRecord, FollowEventRecord
from app.services.auth_service import get_current_authenticated_user
from app.api.v0.endpoints.users import _invalidate_popular

router = APIRouter()

@router.post("/{target_id}/follow")
async def follow_user(
    target_id: uuid.UUID,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    if target_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")
        
    target = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == target_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
        
    current_follow = db.query(FollowCurrentRecord).filter(
        FollowCurrentRecord.actor_user_id == current_user.user_id,
        FollowCurrentRecord.target_user_id == target_id
    ).first()
    
    if current_follow:
        return {"status": "already following"}
        
    db.add(FollowCurrentRecord(actor_user_id=current_user.user_id, target_user_id=target_id))
    db.add(FollowEventRecord(actor_user_id=current_user.user_id, target_user_id=target_id, action="follow"))
    
    current_user.following_count += 1
    target.follower_count += 1
    db.commit()
    await _invalidate_popular()
    return {"status": "followed"}

@router.post("/{target_id}/unfollow")
async def unfollow_user(
    target_id: uuid.UUID,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    current_follow = db.query(FollowCurrentRecord).filter(
        FollowCurrentRecord.actor_user_id == current_user.user_id,
        FollowCurrentRecord.target_user_id == target_id
    ).first()
    
    if not current_follow:
        return {"status": "not following"}
        
    db.delete(current_follow)
    db.add(FollowEventRecord(actor_user_id=current_user.user_id, target_user_id=target_id, action="unfollow"))
    
    target = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == target_id).first()
    current_user.following_count = max(0, current_user.following_count - 1)
    if target:
        target.follower_count = max(0, target.follower_count - 1)
        
    db.commit()
    await _invalidate_popular()
    return {"status": "unfollowed"}