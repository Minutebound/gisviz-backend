from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.database import get_users_db
from app.db.models import PlatformUserRecord, PostRecord # Import your actual models

router = APIRouter()

@router.get("/sitemap-data")
def get_sitemap_data(db: Session = Depends(get_users_db)):
    # Fetch all public posts (adjust filters based on your specific 'published' logic)
    posts = db.query(PostRecord.share_slug, PostRecord.created_timestamp).all()
    
    # Fetch all active users
    users = db.query(PlatformUserRecord.user_handle, PlatformUserRecord.created_timestamp).filter(
        PlatformUserRecord.is_active == 1
    ).all()

    return {
        "posts": [
            {"share_slug": p.share_slug, "last_modified": p.created_timestamp.isoformat()} 
            for p in posts if p.share_slug
        ],
        "users": [
            {"user_handle": u.user_handle, "last_modified": u.created_timestamp.isoformat()} 
            for u in users if u.user_handle
        ]
    }