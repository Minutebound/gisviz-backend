import uuid
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from app.core.config import settings

from app.db.database import (
    auth_engine, spatial_engine,
    AuthSessionLocal, SpatialSessionLocal,
)
from app.db.models import AuthBase, SpatialBase
from app.api.v1.endpoints import auth, posts, seed
from app.services.auth_service import auth_service
from app.services.post_service import post_service

# ---------------------------------------------------------
# 1. Database Initialization
# ---------------------------------------------------------
AuthBase.metadata.create_all(bind=auth_engine)
SpatialBase.metadata.create_all(bind=spatial_engine)

# ---------------------------------------------------------
# 2. Application Setup
# ---------------------------------------------------------
app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 3. DB Session Dependencies
# ---------------------------------------------------------
def get_auth_db():
    db = AuthSessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_spatial_db():
    db = SpatialSessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------
# 4. Route Registration
# ---------------------------------------------------------
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(posts.router, prefix=f"{settings.API_V1_STR}/publications", tags=["Publications"])

# FIXED: Added /system to the prefix so /api/v1/system/seed maps exactly to the router
# Leave the prefix as just the base API string!
app.include_router(seed.router, prefix=f"{settings.API_V1_STR}", tags=["Seed Data"])

# Feed endpoint — matches BOTH /publications and /publications/ to avoid 404s
@app.get(f"{settings.API_V1_STR}/publications", tags=["Publications"])
def get_publication_feed(
    skip: int = 0,
    limit: int = 50,
    spatial_db: Session = Depends(get_spatial_db),
    auth_db: Session = Depends(get_auth_db),
):
    return post_service.retrieve_global_stream(
        spatial_db=spatial_db, auth_db=auth_db, skip=skip, limit=limit
    )

@app.get("/")
def root_health_check():
    return {"status": "operational", "engine": "gisviz-api"}