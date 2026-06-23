from typing import List
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.db.models import (
    FollowEventRecord,
    FollowCurrentRecord,
    PlatformUserRecord,
)


class FollowService:
    def _adjust_counters(self, users_db: Session, actor_id: str, target_id: str, delta: int):
        actor = users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id == actor_id
        ).first()
        target = users_db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_id == target_id
        ).first()
        if actor:
            actor.following_count = max(0, actor.following_count + delta)
        if target:
            target.follower_count = max(0, target.follower_count + delta)

    def follow(self, users_db: Session, actor_id: str, target_id: str):
        if actor_id == target_id:
            raise HTTPException(status_code=400, detail="Cannot follow yourself")

        already = (
            users_db.query(FollowCurrentRecord)
            .filter(
                FollowCurrentRecord.actor_user_id == actor_id,
                FollowCurrentRecord.target_user_id == target_id,
            )
            .first()
        )

        # Always append the event (audit trail), even idempotent re-follows skip counter bump
        users_db.add(
            FollowEventRecord(actor_user_id=actor_id, target_user_id=target_id, action="follow")
        )

        if not already:
            users_db.add(
                FollowCurrentRecord(actor_user_id=actor_id, target_user_id=target_id)
            )
            self._adjust_counters(users_db, actor_id, target_id, +1)

        users_db.commit()
        return {"actor_user_id": actor_id, "target_user_id": target_id, "following": True}

    def unfollow(self, users_db: Session, actor_id: str, target_id: str):
        current = (
            users_db.query(FollowCurrentRecord)
            .filter(
                FollowCurrentRecord.actor_user_id == actor_id,
                FollowCurrentRecord.target_user_id == target_id,
            )
            .first()
        )

        users_db.add(
            FollowEventRecord(actor_user_id=actor_id, target_user_id=target_id, action="unfollow")
        )

        if current:
            users_db.delete(current)
            self._adjust_counters(users_db, actor_id, target_id, -1)

        users_db.commit()
        return {"actor_user_id": actor_id, "target_user_id": target_id, "following": False}

    def followers_of(self, users_db: Session, target_id: str) -> List[FollowCurrentRecord]:
        return (
            users_db.query(FollowCurrentRecord)
            .filter(FollowCurrentRecord.target_user_id == target_id)
            .all()
        )

    def follow_history(self, users_db: Session, actor_id: str, target_id: str) -> List[FollowEventRecord]:
        return (
            users_db.query(FollowEventRecord)
            .filter(
                FollowEventRecord.actor_user_id == actor_id,
                FollowEventRecord.target_user_id == target_id,
            )
            .order_by(FollowEventRecord.occurred_timestamp.asc())
            .all()
        )


follow_service = FollowService()