"""
app/api/v0/endpoints/support.py
================================
Public endpoint — no auth required.
Registered in main.py at /api/v0/support.

POST /support/ticket  — any visitor can submit a support ticket.
"""

import uuid
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.db.database import get_users_db
from app.db.models import SupportTicketRecord
from app.services.auth_service import get_optional_current_user
from app.db.models import PlatformUserRecord

router = APIRouter()

VALID_CATEGORIES = {"bug", "billing", "account", "feature", "other"}


class TicketSubmitPayload(BaseModel):
    contact_email: Optional[str] = None   # required if not logged in
    category:      str                    # bug | billing | account | feature | other
    subject:       str
    description:   str


@router.post("/ticket", status_code=201)
def submit_ticket(
    payload:  TicketSubmitPayload,
    users_db: Session = Depends(get_users_db),
    current_user: Optional[PlatformUserRecord] = Depends(get_optional_current_user),
):
    if payload.category not in VALID_CATEGORIES:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {', '.join(sorted(VALID_CATEGORIES))}",
        )

    # Require an email if no authenticated user
    if current_user is None and not payload.contact_email:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="contact_email is required when not logged in.",
        )

    ticket = SupportTicketRecord(
        ticket_id     = uuid.uuid4(),
        user_id       = current_user.user_id if current_user else None,
        contact_email = payload.contact_email or (
            current_user.email_address if current_user else None
        ),
        category    = payload.category,
        subject     = payload.subject,
        description = payload.description,
        status      = "open",
    )
    users_db.add(ticket)
    users_db.commit()
    users_db.refresh(ticket)

    return {
        "ticket_id": str(ticket.ticket_id),
        "status":    ticket.status,
        "message":   "Your support ticket has been submitted. We'll be in touch shortly.",
    }