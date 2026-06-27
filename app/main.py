import uuid
import os
from datetime import datetime
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import (
    users_engine, posts_engine, UsersSessionLocal, PostsSessionLocal,
    UsersBase, PostsBase, get_users_db, get_posts_db
)
from app.db.models import (
    RoleRecord, PlatformUserRecord, UserLocationRecord, 
    PostRecord, CategoryRecord, KeywordRecord
)
from app.api.v1.endpoints import auth, posts, categories, follows, users, uploads
from app.services.auth_service import auth_service

# 1. Database Initialization
UsersBase.metadata.create_all(bind=users_engine)
PostsBase.metadata.create_all(bind=posts_engine)

# 2. Application Setup
app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Mount Static Uploads Directory (Required for Images)
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# 4. Route Registration
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["Users"])
app.include_router(uploads.router, prefix=f"{settings.API_V1_STR}/uploads", tags=["Uploads"])
app.include_router(posts.router, prefix=f"{settings.API_V1_STR}/posts", tags=["Posts"])
app.include_router(categories.router, prefix=f"{settings.API_V1_STR}/categories", tags=["Categories"])
app.include_router(follows.router, prefix=f"{settings.API_V1_STR}/network", tags=["Social Graph"])


@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api", "version": settings.VERSION}


@app.get("/seed")
def seed_database():
    """Development seed — adapted for the new static-image Post schema."""
    users_db = UsersSessionLocal()
    posts_db = PostsSessionLocal()
    try:
        # ROLES
        role_defs = [
            {"name": "admin", "permissions": {"manage_tags": True, "moderate": True, "publish": True, "admin": True}},
            {"name": "editor", "permissions": {"manage_tags": True, "moderate": True, "publish": True}},
            {"name": "publisher", "permissions": {"publish": True}},
            {"name": "viewer", "permissions": {}},
            {"name": "support", "permissions": {"view_reports": True}},
        ]
        role_map = {}
        for rd in role_defs:
            role = users_db.query(RoleRecord).filter(RoleRecord.name == rd["name"]).first()
            if not role:
                role = RoleRecord(name=rd["name"], permissions=rd["permissions"])
                users_db.add(role)
                users_db.commit()
                users_db.refresh(role)
            role_map[rd["name"]] = role

        # USERS
        admin_user = users_db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == "system_admin").first()
        if not admin_user:
            admin_user = PlatformUserRecord(
                user_id=uuid.uuid4(),
                user_handle="system_admin",
                email_address="admin@gisviz.com",
                hashed_security_password=auth_service.get_password_hash("Password123!"),
                title="Lead Architect",
                role_id=role_map["admin"].role_id,
            )
            users_db.add(admin_user)
            users_db.commit()
            users_db.refresh(admin_user)
            
            # Setting initial location to Boulder, Colorado for local context testing
            loc = UserLocationRecord(user_id=admin_user.user_id, place="Boulder", state="Colorado", country="United States", formatted_string="Boulder, Colorado, United States")
            users_db.add(loc)
            users_db.commit()

        # CATEGORIES
        cat = posts_db.query(CategoryRecord).filter(CategoryRecord.slug == "dem").first()
        if not cat:
            cat = CategoryRecord(slug="dem", label="DEM")
            posts_db.add(cat)
            posts_db.commit()

        return {"status": "Success", "message": "Database seeded with new schema constraints."}

    except Exception as e:
        users_db.rollback()
        posts_db.rollback()
        return {"status": "Error", "details": str(e)}
    finally:
        users_db.close()
        posts_db.close()