import os
import shutil
import uuid
import io
from PIL import Image
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_users_db
from app.db.models import PlatformUserRecord
from app.services.auth_service import get_current_authenticated_user

router = APIRouter()

# Define base paths
BASE_UPLOAD_DIR = "uploads"
AVATAR_DIR = os.path.join(BASE_UPLOAD_DIR, "avatars")
BANNER_DIR = os.path.join(BASE_UPLOAD_DIR, "banners")
VISUAL_DIR = os.path.join(BASE_UPLOAD_DIR, "visuals")

os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(BANNER_DIR, exist_ok=True)
os.makedirs(VISUAL_DIR, exist_ok=True)

@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
    db: Session = Depends(get_users_db),
):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # 1. Temporarily store the old path (do not delete it yet!)
    old_avatar_path = current_user.avatar_path

    # 2. Save the new file
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"{current_user.user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(AVATAR_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 3. Commit the new path to the database
    db_path = f"/uploads/avatars/{filename}"
    current_user.avatar_path = db_path
    db.commit()

    # 4. Only delete the old file AFTER the new one is successfully committed
    if old_avatar_path:
        old_relative = old_avatar_path.lstrip("/")
        if os.path.exists(old_relative):
            try:
                os.remove(old_relative)
            except OSError as e:
                print(f"[uploads] Could not delete old avatar: {e}")

    return {"message": "Avatar uploaded successfully", "avatar_path": db_path}


@router.post("/banner")
async def upload_banner(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
    db: Session = Depends(get_users_db),
):
    """Uploads a profile banner image, deleting the previous one if it exists."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # 1. Temporarily store the old path (do not delete it yet!)
    old_banner_path = current_user.banner_path

    # 2. Save the new file
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"banner_{current_user.user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(BANNER_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 3. Commit the new path to the database
    db_path = f"/uploads/banners/{filename}"
    current_user.banner_path = db_path
    db.commit()

    # 4. Only delete the old file AFTER the new one is successfully committed
    if old_banner_path:
        old_relative = old_banner_path.lstrip("/")
        old_full_path = os.path.join(old_relative)
        if os.path.exists(old_full_path):
            try:
                os.remove(old_full_path)
            except OSError as e:
                print(f"[uploads] Could not delete old banner: {e}")

    return {"message": "Banner uploaded successfully", "banner_path": db_path}