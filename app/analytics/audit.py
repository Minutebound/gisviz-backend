"""
app/analytics/audit.py

Thin synchronous helpers for recording control-panel actions into admin_db.
Call from any admin endpoint AFTER the primary mutation commits.
"""

import uuid
from typing import Optional
from sqlalchemy.orm import Session

from app.db.analytics_models import AdminActionLog, RoleChangeHistory, ReportResolution


def log_admin_action(
    admin_db: Session,
    *,
    admin_user_id,
    action_type: str,
    admin_handle: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id=None,
    payload: Optional[dict] = None,
    ip_address: Optional[str] = None,
    commit: bool = True,
) -> AdminActionLog:
    row = AdminActionLog(
        action_id=uuid.uuid4(),
        admin_user_id=admin_user_id,
        admin_handle=admin_handle,
        action_type=action_type,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        payload=payload or {},
        ip_address=ip_address,
    )
    admin_db.add(row)
    if commit:
        admin_db.commit()
    return row


def log_role_change(
    admin_db: Session,
    *,
    subject_user_id,
    changed_by_user_id,
    new_role: str,
    old_role: Optional[str] = None,
    reason: Optional[str] = None,
    commit: bool = True,
) -> RoleChangeHistory:
    row = RoleChangeHistory(
        change_id=uuid.uuid4(),
        subject_user_id=subject_user_id,
        changed_by_user_id=changed_by_user_id,
        old_role=old_role,
        new_role=new_role,
        reason=reason,
    )
    admin_db.add(row)
    if commit:
        admin_db.commit()
    return row


def log_report_resolution(
    admin_db: Session,
    *,
    report_id,
    resolved_by_user_id,
    resolution: str,
    post_id=None,
    notes: Optional[str] = None,
    commit: bool = True,
) -> ReportResolution:
    row = ReportResolution(
        resolution_id=uuid.uuid4(),
        report_id=report_id,
        post_id=post_id,
        resolved_by_user_id=resolved_by_user_id,
        resolution=resolution,
        notes=notes,
    )
    admin_db.add(row)
    if commit:
        admin_db.commit()
    return row