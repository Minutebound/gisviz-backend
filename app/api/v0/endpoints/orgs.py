"""
app/api/v0/endpoints/orgs.py
============================
Organisation accounts. An organisation is a GisViz account (account_type = organization) registered with an
email address on the organisation's own domain. An admin checks the domain; once VERIFIED, every user whose
verified email is on that domain is a member: what is shared with the organisation reaches all of them (the
"Shared" tab), and the organisation's own private posts and datasets are visible to its members.

  POST   /orgs                       {"name": "..."}  turn MY account into an organisation account (pending review)
  DELETE /orgs/mine                  back to an individual account (members lose access through it)
  GET    /orgs/mine                  my organisation account (if any) and the organisations I am a member of
  GET    /orgs/by-handle/{handle}    public card of a verified organisation (profile badge)
  GET    /orgs?status=pending        admin: organisations to review
  PUT    /orgs/{org_id}/status       admin: {"status": "verified" | "rejected" | "pending"}
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_admin_db, get_users_db
from app.db.models import OrganizationMemberRecord, OrganizationRecord, PlatformUserRecord, log_admin_action
from app.services import access_service as acc
from app.services.auth_service import RoleChecker, get_current_authenticated_user

router = APIRouter()


class OrgCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)


class OrgStatus(BaseModel):
    status: str = Field(..., pattern="^(verified|rejected|pending)$")


def _card(users_db, org: OrganizationRecord, with_private: bool = False) -> dict:
    acct = users_db.get(PlatformUserRecord, org.account_user_id)
    n = users_db.query(func.count()).select_from(OrganizationMemberRecord).filter(
        OrganizationMemberRecord.org_id == org.org_id).scalar() or 0
    out = {"org_id": str(org.org_id), "name": org.name, "email_domain": org.email_domain, "status": org.status,
           "handle": acct.user_handle if acct else None, "member_count": int(n),
           "created_at": org.created_timestamp.isoformat() if org.created_timestamp else None}
    if with_private:
        out["reviewed_at"] = org.reviewed_timestamp.isoformat() if org.reviewed_timestamp else None
        out["account_email"] = acct.email_address if acct else None
    return out


@router.post("", status_code=201)
def create_org(body: OrgCreate, users_db: Session = Depends(get_users_db),
               user: PlatformUserRecord = Depends(get_current_authenticated_user)):
    dom = acc.email_domain(user.email_address)
    if not dom or dom in acc.FREE_MAIL_DOMAINS:
        raise HTTPException(status_code=422, detail="Use an account whose email is on your organisation's own domain "
                                                    "(not a public mail provider such as Gmail or Outlook)")
    if (user.account_type or "individual") == "organization" or acc.org_of_account(users_db, user.user_id):
        raise HTTPException(status_code=409, detail="This account is already an organisation account")
    if users_db.query(OrganizationRecord).filter(OrganizationRecord.email_domain == dom).first():
        raise HTTPException(status_code=409, detail=f"An organisation is already registered for @{dom}")
    org = OrganizationRecord(account_user_id=user.user_id, name=body.name.strip(), email_domain=dom, status="pending")
    user.account_type = "organization"
    users_db.add(org)
    users_db.commit()
    users_db.refresh(org)
    return _card(users_db, org, with_private=True)


@router.delete("/mine")
def leave_org_account(users_db: Session = Depends(get_users_db),
                      user: PlatformUserRecord = Depends(get_current_authenticated_user)):
    org = acc.org_of_account(users_db, user.user_id)
    if org is None:
        raise HTTPException(status_code=404, detail="This account is not an organisation account")
    acc.drop_org_members(users_db, org)
    users_db.delete(org)
    user.account_type = "individual"
    users_db.commit()
    acc.sync_memberships(users_db, user)
    return {"account_type": "individual"}


@router.get("/mine")
def my_orgs(users_db: Session = Depends(get_users_db),
            user: PlatformUserRecord = Depends(get_current_authenticated_user)):
    acc.sync_memberships(users_db, user)                      # the email may have changed since the last visit
    own = acc.org_of_account(users_db, user.user_id)
    rows = (users_db.query(OrganizationRecord, OrganizationMemberRecord.role)
            .join(OrganizationMemberRecord, OrganizationMemberRecord.org_id == OrganizationRecord.org_id)
            .filter(OrganizationMemberRecord.user_id == user.user_id).all())
    return {"account_type": user.account_type or "individual",
            "organization": _card(users_db, own, with_private=True) if own else None,
            "memberships": [{**_card(users_db, o), "role": role} for o, role in rows if o.status == "verified"],
            "email_domain": acc.email_domain(user.email_address)}


@router.get("/by-handle/{handle}")
def org_by_handle(handle: str, users_db: Session = Depends(get_users_db)):
    acct = users_db.query(PlatformUserRecord).filter(func.lower(PlatformUserRecord.user_handle) == handle.lower()).first()
    org = acc.org_of_account(users_db, acct.user_id) if acct else None
    if org is None or org.status != "verified":
        raise HTTPException(status_code=404, detail="Not a verified organisation")
    return _card(users_db, org)


@router.get("")
def list_orgs(status: Optional[str] = Query(None, pattern="^(verified|rejected|pending)$"),
              users_db: Session = Depends(get_users_db),
              _admin: PlatformUserRecord = Depends(RoleChecker(["admin"]))):
    q = users_db.query(OrganizationRecord)
    if status:
        q = q.filter(OrganizationRecord.status == status)
    return [_card(users_db, o, with_private=True) for o in q.order_by(OrganizationRecord.created_timestamp.desc()).limit(500)]


@router.put("/{org_id}/status")
def review_org(org_id: str, body: OrgStatus, request: Request,
               users_db: Session = Depends(get_users_db), admin_db: Session = Depends(get_admin_db),
               admin: PlatformUserRecord = Depends(RoleChecker(["admin"]))):
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Organisation not found")
    org = users_db.get(OrganizationRecord, oid)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    org.status = body.status
    org.reviewed_by_user_id = admin.user_id
    org.reviewed_timestamp = dt.datetime.now(dt.timezone.utc)
    users_db.commit()
    added = acc.backfill_org(users_db, org) if body.status == "verified" else 0
    if body.status != "verified":
        acc.drop_org_members(users_db, org)
    try:
        log_admin_action(admin_db, admin_user_id=admin.user_id, admin_handle=admin.user_handle,
                         action_type=f"org.{body.status}", target_type="organization", target_id=str(org.org_id),
                         payload={"name": org.name, "domain": org.email_domain, "members_added": added},
                         ip_address=request.client.host if request.client else None)
    except Exception as e:
        print(f"[orgs] audit log failed: {e}")
    return _card(users_db, org, with_private=True)