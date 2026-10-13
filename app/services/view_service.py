"""
app/services/view_service.py
============================
Post views. Every time a post page is opened (POST /posts/{id}/view) one row is added to post_views
in the POSTS database, and posts.views_count (the running total shown on cards and used for "most
viewed") goes up by one, in the same transaction.

Only a repeat from the same viewer within REPEAT_SECONDS is ignored, so a double page load or a
script hammering the endpoint does not inflate the count; any later open counts again.

viewer_hash: signed-in users by user id, anonymous visitors by IP + user agent, hashed with the
server secret, so no raw IP is stored. It lets the daily stats count distinct viewers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Optional

from sqlalchemy import update

from app.core.config import settings
from app.db.database import PostsSessionLocal
from app.db.models import PostRecord, PostViewRecord

REPEAT_SECONDS = 10


def viewer_hash(user_id: Optional[str], ip: Optional[str], user_agent: Optional[str]) -> str:
    raw = f"u:{user_id}" if user_id else f"a:{ip or ''}|{(user_agent or '')[:300]}"
    return hashlib.sha256(f"{settings.JWT_SECRET_KEY}|{raw}".encode()).hexdigest()


def record_view(post_id: str, user_id: Optional[str], ip: Optional[str], user_agent: Optional[str],
                now: Optional[dt.datetime] = None) -> dict:
    """Count one view of an active post. Returns {"views_count": n, "counted": bool}; raises
    LookupError when the post does not exist or is not active."""
    pid = uuid.UUID(str(post_id))
    now = now or dt.datetime.now(dt.timezone.utc)
    vh = viewer_hash(user_id, ip, user_agent)
    with PostsSessionLocal() as db:
        post = db.get(PostRecord, pid)
        if post is None or not post.is_active:
            raise LookupError(post_id)
        repeat = (db.query(PostViewRecord.view_id)
                  .filter(PostViewRecord.post_id == pid, PostViewRecord.viewer_hash == vh,
                          PostViewRecord.viewed_timestamp > now - dt.timedelta(seconds=REPEAT_SECONDS),
                          PostViewRecord.viewed_timestamp <= now)
                  .first())
        if repeat is not None:
            return {"views_count": post.views_count or 0, "counted": False}
        db.add(PostViewRecord(post_id=pid, user_id=uuid.UUID(str(user_id)) if user_id else None,
                              viewer_hash=vh, viewed_timestamp=now))
        db.flush()
        n = db.execute(update(PostRecord).where(PostRecord.post_id == pid)
                       .values(views_count=PostRecord.views_count + 1)
                       .returning(PostRecord.views_count)).scalar()
        db.commit()
    return {"views_count": n, "counted": True}