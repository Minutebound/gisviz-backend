from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_users_db, get_posts_db
from app.db.models import PlatformUserRecord, PostRecord

router = APIRouter()


@router.get("/sitemap-data")
def get_sitemap_data(
    users_db: Session = Depends(get_users_db),
    posts_db: Session = Depends(get_posts_db),
):
    posts = (
        posts_db.query(PostRecord.share_slug, PostRecord.updated_timestamp)
        .filter(
            PostRecord.share_slug.isnot(None),
            PostRecord.is_active == 1,          # only index active posts
        )
        .all()
    )

    users = (
        users_db.query(PlatformUserRecord.user_handle, PlatformUserRecord.updated_timestamp)
        .filter(PlatformUserRecord.is_active == 1)
        .all()
    )

    return {
        "posts": [
            {
                "share_slug": p.share_slug,
                "last_modified": p.updated_timestamp.isoformat() if p.updated_timestamp else None,
            }
            for p in posts
        ],
        "users": [
            {
                "user_handle": u.user_handle,
                "last_modified": u.updated_timestamp.isoformat() if u.updated_timestamp else None,
            }
            for u in users
        ],
    }