import uuid
import re
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, validator
from typing import Optional, List

from app.db.database import get_users_db, get_admin_db          # ← get_admin_db added
from app.db.models import PlatformUserRecord, RoleRecord, UserLocationRecord, FollowCurrentRecord
from app.services.auth_service import get_current_authenticated_user, RoleChecker, auth_service, get_optional_current_user
from app.schemas.user_schema import UserSettingsUpdatePayload
from app.db.models import log_admin_action, log_role_change

router = APIRouter()


# ── Inline request schemas ───────────────────────────────────────────

class HandleUpdatePayload(BaseModel):
    new_handle: str

    @validator("new_handle")
    def validate_handle(cls, v: str) -> str:
        v = v.strip().lstrip("@")
        if len(v) < 3:
            raise ValueError("Handle must be at least 3 characters")
        if len(v) > 30:
            raise ValueError("Handle must be 30 characters or fewer")
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError("Handle may only contain letters, numbers, and underscores")
        return v


class EmailChangeRequestPayload(BaseModel):
    new_email: EmailStr
    current_password: str


class EmailChangeVerifyPayload(BaseModel):
    new_email: EmailStr
    otp: str


class DeleteAccountPayload(BaseModel):
    current_password: str


class RoleUpdatePayload(BaseModel):
    role_name: str  # "admin" | "editor" | "publisher" | "viewer" | "support"


# ── Shared helper ────────────────────────────────────────────────────

def _full_user_dict(user: PlatformUserRecord) -> dict:
    loc = user.location
    return {
        "user_id": str(user.user_id),
        "user_handle": user.user_handle,
        "email_address": user.email_address,
        "is_verified": bool(user.is_verified),
        "is_active": bool(user.is_active),
        "avatar_path": user.avatar_path,
        "banner_path": getattr(user, "banner_path", None),
        "title": user.title,
        "linkedin_url": user.linkedin_url,
        "medium_url": user.medium_url,
        "website_url": user.website_url,
        "location": {
            "place": loc.place if loc else None,
            "state": loc.state if loc else None,
            "country": loc.country if loc else None,
            "formatted_string": loc.formatted_string if loc else None,
        },
        "follower_count": user.follower_count,
        "following_count": user.following_count,
        "post_count": user.post_count,
        "role_name": user.role.name if user.role else "viewer",
        "joined_at": user.created_timestamp,
    }


# ── GET /me ──────────────────────────────────────────────────────────

@router.get("/me")
def get_my_profile(
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return _full_user_dict(current_user)


# ── GET /all — ADMIN ONLY ────────────────────────────────────────────

@router.get("/all")
def list_all_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    query = db.query(PlatformUserRecord)
    if q:
        query = query.filter(
            (PlatformUserRecord.user_handle.ilike(f"%{q}%")) |
            (PlatformUserRecord.email_address.ilike(f"%{q}%"))
        )
    users = query.order_by(PlatformUserRecord.created_timestamp.desc()).offset(skip).limit(limit).all()
    total = db.query(PlatformUserRecord).count()
    return {
        "total": total,
        "users": [_full_user_dict(u) for u in users],
    }


# ── GET /profile/{handle} ────────────────────────────────────────────

@router.get("/profile/{handle}")
def get_user_profile(
    handle: str,
    current_user_id: str = Query(None),
    db: Session = Depends(get_users_db),
):
    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle == handle
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    is_following = False
    if current_user_id:
        try:
            uuid_obj = uuid.UUID(current_user_id)
            fr = db.query(FollowCurrentRecord).filter(
                FollowCurrentRecord.actor_user_id == uuid_obj,
                FollowCurrentRecord.target_user_id == user.user_id,
            ).first()
            if fr:
                is_following = True
        except ValueError:
            pass

    profile = _full_user_dict(user)
    profile["is_following"] = is_following
    return profile


# ── GET /popular ─────────────────────────────────────────────────────

@router.get("/popular")
def get_popular_publishers(
    limit: int = Query(50, ge=1, le=100),
    current_user_id: str = Query(None),
    db: Session = Depends(get_users_db),
):
    users = (
        db.query(PlatformUserRecord)
        .filter(PlatformUserRecord.post_count > 0)
        .order_by(PlatformUserRecord.follower_count.desc())
        .limit(limit).all()
    )
    results = []
    for u in users:
        is_followed = False
        if current_user_id:
            try:
                uuid_obj = uuid.UUID(current_user_id)
                fr = db.query(FollowCurrentRecord).filter(
                    FollowCurrentRecord.actor_user_id == uuid_obj,
                    FollowCurrentRecord.target_user_id == u.user_id,
                ).first()
                is_followed = fr is not None
            except ValueError:
                pass
        results.append({
            "user_id": str(u.user_id),
            "user_handle": u.user_handle,
            "avatar_path": u.avatar_path,
            "title": u.title,
            "follower_count": u.follower_count,
            "post_count": u.post_count,
            "is_followed": is_followed,
        })
    return results


# ── PUT /settings ────────────────────────────────────────────────────

@router.put("/settings")
def update_settings(
    payload: UserSettingsUpdatePayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    current_user.title        = payload.title
    current_user.linkedin_url = payload.linkedin_url
    current_user.medium_url   = payload.medium_url
    current_user.website_url  = payload.website_url

    loc = db.query(UserLocationRecord).filter(
        UserLocationRecord.user_id == current_user.user_id
    ).first()
    if loc:
        loc.place   = payload.place
        loc.state   = payload.state
        loc.country = payload.country
        loc.formatted_string = f"{payload.place}, {payload.state}, {payload.country}".strip(", ")
    else:
        db.add(UserLocationRecord(
            user_id=current_user.user_id,
            place=payload.place,
            state=payload.state,
            country=payload.country,
            formatted_string=f"{payload.place}, {payload.state}, {payload.country}".strip(", "),
        ))
    db.commit()
    return _full_user_dict(current_user)


# ── PUT /handle ──────────────────────────────────────────────────────

@router.put("/handle")
def update_handle(
    payload: HandleUpdatePayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    if db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_handle == payload.new_handle,
        PlatformUserRecord.user_id != current_user.user_id,
    ).first():
        raise HTTPException(status_code=409, detail="Handle already taken")
    current_user.user_handle = payload.new_handle
    db.commit()
    return {"message": "Handle updated", "user_handle": payload.new_handle}


# ── POST /email/request ──────────────────────────────────────────────

@router.post("/email/request")
def request_email_change(
    payload: EmailChangeRequestPayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    if not auth_service.verify_password(payload.current_password, current_user.hashed_security_password):
        raise HTTPException(status_code=400, detail="Incorrect password")

    import random, string
    otp = "".join(random.choices(string.digits, k=6))
    current_user.verification_otp     = otp
    current_user.otp_expires_at       = datetime.now(timezone.utc) + timedelta(minutes=15)
    current_user.password_reset_token = f"email_change::{payload.new_email}"
    db.commit()

    auth_service.simulate_send_email(
        email=payload.new_email,
        subject="Confirm your new email address",
        content=f"Your verification code is: {otp}. It expires in 15 minutes.",
    )
    return {"message": "Verification code sent", "dev_otp": otp}


# ── POST /email/verify ───────────────────────────────────────────────

@router.post("/email/verify")
def verify_email_change(
    payload: EmailChangeVerifyPayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    pending_token = current_user.password_reset_token or ""
    if not pending_token.startswith("email_change::"):
        raise HTTPException(status_code=400, detail="No email change in progress")

    pending_email = pending_token.removeprefix("email_change::")
    if pending_email != payload.new_email:
        raise HTTPException(status_code=400, detail="Email mismatch. Please start the process again.")
    if current_user.verification_otp != payload.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code")
    if current_user.otp_expires_at and datetime.now(timezone.utc) > current_user.otp_expires_at:
        raise HTTPException(status_code=400, detail="Code has expired. Please request a new one.")

    current_user.email_address        = pending_email
    current_user.verification_otp     = None
    current_user.otp_expires_at       = None
    current_user.password_reset_token = None
    db.commit()
    return {"message": "Email updated successfully", "new_email": pending_email}


# ── DELETE /me ───────────────────────────────────────────────────────

@router.delete("/me")
def delete_own_account(
    payload: DeleteAccountPayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    if not auth_service.verify_password(payload.current_password, current_user.hashed_security_password):
        raise HTTPException(status_code=400, detail="Incorrect password — deactivation cancelled")
    current_user.is_active = 0
    db.commit()
    return {"message": "Account deactivated successfully"}


# ── PUT /{user_id}/role  — ADMIN ONLY  (+ audit log) ────────────────

@router.put("/{user_id}/role")
def update_user_role(
    user_id: uuid.UUID,
    payload: RoleUpdatePayload,
    request: Request,
    db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = user.role.name if user.role else None

    role = db.query(RoleRecord).filter(RoleRecord.name == payload.role_name).first()
    if not role:
        raise HTTPException(status_code=400, detail=f"Unknown role '{payload.role_name}'.")

    user.role_id = role.role_id
    db.commit()

    log_role_change(
        admin_db,
        subject_user_id=user.user_id,
        changed_by_user_id=current_user.user_id,
        old_role=old_role,
        new_role=payload.role_name,
    )
    return {"message": "Role updated successfully", "user_handle": user.user_handle, "new_role": payload.role_name}


# ── PUT /{user_id}/status  — ADMIN/SUPPORT  (+ audit log) ───────────

@router.put("/{user_id}/status")
def set_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    request: Request,
    db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "support"])),
):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = int(is_active)
    db.commit()

    log_admin_action(
        admin_db,
        admin_user_id=current_user.user_id,
        admin_handle=current_user.user_handle,
        action_type="user.status_change",
        target_type="user",
        target_id=str(user_id),
        payload={"user_handle": user.user_handle, "is_active": int(is_active)},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "message": f"User status set to {'active' if is_active else 'deactivated'}",
        "is_active": int(is_active),
    }


# ── DELETE /{user_id}  — ADMIN ONLY  (+ audit log) ──────────────────

@router.delete("/{user_id}")
def hard_delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_users_db),
    admin_db: Session = Depends(get_admin_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account here.")

    # Snapshot before deleting — the source row will be gone after commit
    snapshot = {
        "user_handle": user.user_handle,
        "email": user.email_address,
        "role": user.role.name if user.role else None,
    }
    db.delete(user)
    db.commit()

    log_admin_action(
        admin_db,
        admin_user_id=current_user.user_id,
        admin_handle=current_user.user_handle,
        action_type="user.delete",
        target_type="user",
        target_id=str(user_id),
        payload=snapshot,
        ip_address=request.client.host if request.client else None,
    )
    return {"message": f"User {snapshot['user_handle']} permanently deleted"}

## --Support Tickets management endpoints ---
from app.db.models import SupportTicketRecord
from app.schemas.user_schema import SupportTicketPayload, SupportTicketResponse, SupportStatusPayload

# ── POST /support (Users creating tickets) ───────────────────────────
@router.post("/support", response_model=SupportTicketResponse)
def create_support_ticket(
    payload: SupportTicketPayload,
    db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    ticket = SupportTicketRecord(
        user_id=current_user.user_id if current_user else None,
        contact_email=payload.contact_email or (current_user.email_address if current_user else None),
        category=payload.category,
        subject=payload.subject,
        description=payload.description
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    
    res = SupportTicketResponse.model_validate(ticket)
    if current_user:
        res.user_handle = current_user.user_handle
    return res

# ── GET /support/all (Admins viewing tickets) ────────────────────────
@router.get("/support/all", response_model=List[SupportTicketResponse])
def get_all_support_tickets(
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor", "support"])),
):
    tickets = db.query(SupportTicketRecord).order_by(SupportTicketRecord.created_timestamp.desc()).all()
    results = []
    for t in tickets:
        dto = SupportTicketResponse.model_validate(t)
        if t.user:
            dto.user_handle = t.user.user_handle
        results.append(dto)
    return results

# ── PUT /support/{ticket_id}/status (Admins resolving tickets) ───────
@router.put("/support/{ticket_id}/status")
def update_support_status(
    ticket_id: uuid.UUID,
    payload: SupportStatusPayload,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(RoleChecker(["admin", "editor", "support"])),
):
    if payload.status not in ("open", "resolved", "dismissed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    ticket = db.query(SupportTicketRecord).filter(SupportTicketRecord.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = payload.status
    db.commit()
    return {"status": payload.status, "ticket_id": str(ticket_id)}