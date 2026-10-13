"""
app/api/v0/endpoints/regions.py
===============================
Regions (MISC database). Used by the dataset region dropdown on /admin/datasets, and available to
other features later.

  GET    /regions                 active regions, ordered (public)        ?all=true includes inactive (admin)
  POST   /regions                 add a region                            (admin)
  PUT    /regions/{code}          edit a region                           (admin)
  DELETE /regions/{code}          delete (refused while datasets or sub-regions use it)  (admin)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_admin_db
from app.db.models import PlatformUserRecord, log_admin_action
from app.services import catalog_service, region_service
from app.services.auth_service import RoleChecker, get_optional_current_user

router = APIRouter()
require_admin = RoleChecker(["admin"])


class RegionIn(BaseModel):
    code: Optional[str] = Field(None, max_length=60)
    name: Optional[str] = Field(None, max_length=120)
    kind: Optional[str] = Field(None, max_length=20)
    parent_code: Optional[str] = Field(None, max_length=60)
    iso_a2: Optional[str] = Field(None, max_length=2)
    iso_a3: Optional[str] = Field(None, max_length=3)
    m49: Optional[str] = Field(None, max_length=3)
    bbox: Optional[list[float]] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


def _audit(admin_db, user, request: Request, action: str, code: str, payload: Optional[dict] = None):
    try:
        log_admin_action(admin_db, admin_user_id=user.user_id, admin_handle=getattr(user, "user_handle", None),
                         action_type=action, target_type="region", target_id=code, payload=payload or {},
                         ip_address=request.client.host if request.client else None)
    except Exception as e:
        print(f"[regions] audit log failed: {e}")


@router.get("/")
def list_regions(all: bool = Query(False), user=Depends(get_optional_current_user)):
    is_admin = bool(user) and getattr(getattr(user, "role", None), "name", None) == "admin"
    return region_service.list_regions(include_inactive=all and is_admin)


@router.post("/", status_code=201)
def create_region(body: RegionIn, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_admin)):
    try:
        row = region_service.create(body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "region.create", row["code"], {"name": row["name"]})
    return row


@router.put("/{code}")
def update_region(code: str, body: RegionIn, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_admin)):
    changes = body.model_dump(exclude_unset=True)
    changes.pop("code", None)                      # the code is the stable key; it never changes
    try:
        row = region_service.update(code, changes)
    except LookupError:
        raise HTTPException(status_code=404, detail="Region not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _audit(admin_db, user, request, "region.update", code, {"fields": sorted(changes)})
    return row


@router.delete("/{code}")
def delete_region(code: str, request: Request, admin_db: Session = Depends(get_admin_db),
                  user: PlatformUserRecord = Depends(require_admin)):
    used = catalog_service.region_usage(code)
    if used:
        raise HTTPException(status_code=409, detail=f"{used} dataset(s) use this region; change them first")
    try:
        region_service.delete(code)
    except LookupError:
        raise HTTPException(status_code=404, detail="Region not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _audit(admin_db, user, request, "region.delete", code)
    return {"deleted": code}