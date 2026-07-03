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
    """Uploads a profile picture and updates the user's database record."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"{current_user.user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(AVATAR_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    db_path = f"/uploads/avatars/{filename}"
    current_user.avatar_path = db_path
    db.commit()

    return {"message": "Avatar uploaded successfully", "avatar_path": db_path}


@router.post("/visual")
async def upload_visual(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    """
    Uploads a publication visual, ensures minimum HD quality, and saves it.
    Images smaller than 1080×1920 are upscaled. Output is always JPEG.
    """
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    content = await file.read()

    try:
        img = Image.open(io.BytesIO(content))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file format")

    # Convert to RGB (strips transparency from PNGs so they save cleanly as JPEG)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Upscale if below minimum HD dimensions
    MIN_WIDTH = 1080
    MIN_HEIGHT = 1920

    if img.width < MIN_WIDTH or img.height < MIN_HEIGHT:
        width_ratio = MIN_WIDTH / img.width
        height_ratio = MIN_HEIGHT / img.height
        scale_factor = max(width_ratio, height_ratio)
        new_width = int(img.width * scale_factor)
        new_height = int(img.height * scale_factor)
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    filename = f"post_{uuid.uuid4().hex}.jpg"
    file_path = os.path.join(VISUAL_DIR, filename)
    img.save(file_path, format="JPEG", quality=85, optimize=True)

    db_path = f"/uploads/visuals/{filename}"
    return {"message": "Visual processed and uploaded successfully", "visual_path": db_path}


@router.post("/banner")
async def upload_banner(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
    db: Session = Depends(get_users_db),
):
    """Uploads a profile banner image."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"banner_{current_user.user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(BANNER_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    db_path = f"/uploads/banners/{filename}"
    current_user.banner_path = db_path
    db.commit()

    return {"message": "Banner uploaded successfully", "banner_path": db_path}