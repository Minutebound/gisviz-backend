"""
app/api/v1/endpoints/admin_access.py

Additive router for the access-control UI. Provides:
  GET /admin/access/pages           -> the page registry + permission catalog
  GET /admin/access/matrix          -> derived page x role access grid

Role CRUD already exists in admin.py (/admin/roles) and is reused as-is.
This router only adds the page/matrix reads — it does not duplicate role writes.

Register in main.py after the admin router:
    from app.api.v1.endpoints import admin_access
    app.include_router(
        admin_access.router,
        prefix=f"{settings.API_V1_STR}/admin",
        tags=["Admin Access"],
    )
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_users_db
from app.db.models import PlatformUserRecord, RoleRecord
from app.services.auth_service import RoleChecker
from app.core.page_registry import (
    PAGE_REGISTRY, PERMISSION_CATALOG, role_can_access,
)

router = APIRouter()

ADMIN = RoleChecker(["admin"])


@router.get("/access/pages")
def list_pages(_: PlatformUserRecord = Depends(ADMIN)):
    """The page registry + the permission catalog the UI toggles."""
    return {
        "pages": [p.model_dump() for p in PAGE_REGISTRY],
        "permissions": PERMISSION_CATALOG,
    }


@router.get("/access/matrix")
def access_matrix(
    users_db: Session = Depends(get_users_db),
    _: PlatformUserRecord = Depends(ADMIN),
):
    """
    Derived page x role access grid.
    Each cell = can a role (via its permissions) reach a page?
    This is READ-ONLY and always consistent with the role permissions —
    change a role's perms and the matrix updates automatically.
    """
    roles = users_db.query(RoleRecord).order_by(RoleRecord.role_id).all()

    role_cols = [
        {"role_id": r.role_id, "name": r.name, "user_count": len(r.users)}
        for r in roles
    ]

    rows = []
    for page in PAGE_REGISTRY:
        access = {
            r.role_id: role_can_access(r.permissions or {}, page.required_permission)
            for r in roles
        }
        rows.append({
            "key": page.key,
            "label": page.label,
            "path": page.path,
            "required_permission": page.required_permission,
            "description": page.description,
            "access": access,   # { role_id: bool }
        })

    return {"roles": role_cols, "pages": rows}