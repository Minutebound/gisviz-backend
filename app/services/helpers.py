import secrets
import string
from sqlalchemy.orm import Session
from app.db.models import PostRecord

def generate_unique_slug(db: Session, length: int = 8) -> str:
    """Generates a random, unique slug for post sharing."""
    while True:
        # Generate a random URL-safe string
        slug = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))
        
        # Check if it already exists in the database
        exists = db.query(PostRecord).filter(PostRecord.share_slug == slug).first()
        
        # If it doesn't exist, we have a unique slug
        if not exists:
            return slug