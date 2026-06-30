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
BANNER_DIR = os.path.join(BASE_UPLOAD_DIR, "banners") # NEW
VISUAL_DIR = os.path.join(BASE_UPLOAD_DIR, "visuals")

os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(BANNER_DIR, exist_ok=True)
os.makedirs(VISUAL_DIR, exist_ok=True)

@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
    db: Session = Depends(get_users_db)
):
    """Uploads a profile picture and updates the user's database record."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # Generate a unique filename: user_id + random string + extension
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"{current_user.user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(AVATAR_DIR, filename)

    # Save the file
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # The URL path we will store in the database (e.g., /uploads/avatars/filename.jpg)
    db_path = f"/uploads/avatars/{filename}"
    
    # Update the user record
    current_user.avatar_path = db_path
    db.commit()

    return {"message": "Avatar uploaded successfully", "avatar_path": db_path}


@router.post("/visual")
async def upload_visual(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    """Uploads a publication visual and returns the path to be used in the create_post payload."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # Generate a unique filename
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"post_{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(VISUAL_DIR, filename)

    # Save the file
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # The URL path to attach to the PostRecord
    db_path = f"/uploads/visuals/{filename}"

    return {"message": "Visual uploaded successfully", "visual_path": db_path}


@router.post("/visual")
async def upload_visual(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    """Uploads a publication visual, ensures minimum HD quality, and saves it."""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # Read the file into memory
    content = await file.read()
    
    try:
        img = Image.open(io.BytesIO(content))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file format")

    # Convert to RGB (Strips out transparent backgrounds from PNGs so they save cleanly as JPEGs)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Target minimums
    MIN_WIDTH = 1080
    MIN_HEIGHT = 1920

    # If the image is smaller than our minimums, upscale it proportionally
    if img.width < MIN_WIDTH or img.height < MIN_HEIGHT:
        # Calculate the ratio needed to meet the minimums
        width_ratio = MIN_WIDTH / img.width
        height_ratio = MIN_HEIGHT / img.height
        
        # We take the MAXIMUM ratio to ensure BOTH dimensions hit the minimum threshold
        scale_factor = max(width_ratio, height_ratio)
        
        new_width = int(img.width * scale_factor)
        new_height = int(img.height * scale_factor)
        
        # LANCZOS is the highest quality upscaling filter available in Pillow
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Generate a unique filename (Force saving as .jpg for optimization)
    filename = f"post_{uuid.uuid4().hex}.jpg"
    file_path = os.path.join(VISUAL_DIR, filename)

    # Save the processed image with slight compression for web performance
    img.save(file_path, format="JPEG", quality=85, optimize=True)

    # The URL path to attach to the PostRecord
    db_path = f"/uploads/visuals/{filename}"

    return {"message": "Visual processed and uploaded successfully", "visual_path": db_path}

@router.post("/banner")
async def upload_banner(
    file: UploadFile = File(...),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
    db: Session = Depends(get_users_db)
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