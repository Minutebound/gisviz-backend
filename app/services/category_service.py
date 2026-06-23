import re
from datetime import datetime
from typing import List
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.db.models import CategoryRecord, PendingTagRecord


def _slugify(label: str) -> str:
    s = label.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60]


class CategoryService:
    def list_categories(self, posts_db: Session) -> List[CategoryRecord]:
        return (
            posts_db.query(CategoryRecord)
            .order_by(CategoryRecord.usage_count.desc())
            .all()
        )

    def suggest_tag(self, posts_db: Session, label: str, user_id: str) -> PendingTagRecord:
        slug = _slugify(label)
        if not slug:
            raise HTTPException(status_code=400, detail="Invalid tag label")

        # already an approved category?
        if posts_db.query(CategoryRecord).filter(CategoryRecord.slug == slug).first():
            raise HTTPException(status_code=409, detail="Tag already exists as a category")

        # already pending?
        existing = (
            posts_db.query(PendingTagRecord)
            .filter(PendingTagRecord.normalized_slug == slug, PendingTagRecord.status == "pending")
            .first()
        )
        if existing:
            return existing

        pending = PendingTagRecord(
            label=label.strip(),
            normalized_slug=slug,
            suggested_by_user_id=user_id,
            status="pending",
        )
        posts_db.add(pending)
        posts_db.commit()
        posts_db.refresh(pending)
        return pending

    def list_pending(self, posts_db: Session) -> List[PendingTagRecord]:
        return (
            posts_db.query(PendingTagRecord)
            .filter(PendingTagRecord.status == "pending")
            .order_by(PendingTagRecord.created_timestamp.asc())
            .all()
        )

    def approve_tag(self, posts_db: Session, pending_id: str) -> CategoryRecord:
        pending = (
            posts_db.query(PendingTagRecord)
            .filter(PendingTagRecord.pending_id == pending_id)
            .first()
        )
        if not pending or pending.status != "pending":
            raise HTTPException(status_code=404, detail="Pending tag not found")

        category = CategoryRecord(slug=pending.normalized_slug, label=pending.label)
        posts_db.add(category)
        pending.status = "approved"
        pending.reviewed_timestamp = datetime.utcnow()
        posts_db.commit()
        posts_db.refresh(category)
        return category

    def reject_tag(self, posts_db: Session, pending_id: str) -> None:
        pending = (
            posts_db.query(PendingTagRecord)
            .filter(PendingTagRecord.pending_id == pending_id)
            .first()
        )
        if not pending or pending.status != "pending":
            raise HTTPException(status_code=404, detail="Pending tag not found")
        pending.status = "rejected"
        pending.reviewed_timestamp = datetime.utcnow()
        posts_db.commit()


category_service = CategoryService()