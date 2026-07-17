"""
app/db/models.py
================
Single source of truth for every SQLAlchemy model.

Databases:
  UsersBase    → users_db      (users, roles, follows)
  PostsBase    → posts_db      (posts, categories, keywords, likes, comments, reports)
  AnalyticsBase→ analytics_db  (star schema warehouse, snapshot ETL)
  AdminBase    → admin_db      (audit trail, role history, report resolutions)

"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import (
    Column, String, DateTime, Integer, BigInteger, Text,
    Date, ForeignKey, Index, Numeric, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import UsersBase, PostsBase, AnalyticsBase, AdminBase


# ════════════════════════════════════════════════════════════════════
#  USERS DATABASE
# ════════════════════════════════════════════════════════════════════

class RoleRecord(UsersBase):
    __tablename__ = "roles"
    role_id     = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(50), unique=True, nullable=False)
    permissions = Column(JSONB, default=dict, nullable=False)
    users       = relationship("PlatformUserRecord", back_populates="role")


class PlatformUserRecord(UsersBase):
    __tablename__   = "platform_users"
    user_id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_handle     = Column(String(50), unique=True, nullable=False, index=True)
    email_address   = Column(String(255), unique=True, nullable=False, index=True)
    hashed_security_password = Column(String, nullable=False)
    is_verified     = Column(Integer, default=0, nullable=False)
    verification_otp     = Column(String(6), nullable=True)
    otp_expires_at       = Column(DateTime(timezone=True), nullable=True)
    password_reset_token = Column(String(64), nullable=True)
    reset_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    avatar_path     = Column(String, nullable=True)
    banner_path     = Column(String, nullable=True)
    title           = Column(String(100), nullable=True)
    linkedin_url    = Column(String(255), nullable=True)
    medium_url      = Column(String(255), nullable=True)
    website_url     = Column(String(255), nullable=True)
    role_id         = Column(Integer, ForeignKey("roles.role_id"), nullable=True, index=True)
    follower_count  = Column(Integer, default=0, nullable=False)
    following_count = Column(Integer, default=0, nullable=False)
    post_count      = Column(Integer, default=0, nullable=False)
    is_active       = Column(Integer, default=1, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    role     = relationship("RoleRecord", back_populates="users")
    location = relationship("UserLocationRecord", back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserLocationRecord(UsersBase):
    __tablename__ = "user_locations"
    location_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id          = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), unique=True)
    place            = Column(String(100), nullable=True)
    state            = Column(String(100), nullable=True)
    country          = Column(String(100), nullable=True)
    formatted_string = Column(String(255), nullable=True)
    user = relationship("PlatformUserRecord", back_populates="location")


class FollowEventRecord(UsersBase):
    __tablename__   = "follow_events"
    event_id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id   = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_user_id  = Column(UUID(as_uuid=True), nullable=False, index=True)
    action          = Column(String(10), nullable=False)
    occurred_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_follow_events_pair_time", "actor_user_id", "target_user_id", "occurred_timestamp"),
    )


class FollowCurrentRecord(UsersBase):
    __tablename__   = "follows_current"
    actor_user_id   = Column(UUID(as_uuid=True), primary_key=True)
    target_user_id  = Column(UUID(as_uuid=True), primary_key=True)
    followed_since  = Column(DateTime(timezone=True), server_default=func.now())


# ════════════════════════════════════════════════════════════════════
#  POSTS DATABASE
# ════════════════════════════════════════════════════════════════════

class PostRecord(PostsBase):
    __tablename__   = "posts"
    post_id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publisher_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    title           = Column(String(255), nullable=False)
    description     = Column(Text, nullable=True)
    visual_image_path = Column(String, nullable=True)
    note            = Column(Text, nullable=True)
    source_name     = Column(String(255), nullable=True)
    source_url      = Column(String(1024), nullable=True)
    share_slug      = Column(String(32), unique=True, index=True, nullable=False)
    total_likes_count    = Column(Integer, default=0, nullable=False)
    total_comments_count = Column(Integer, default=0, nullable=False)
    is_active       = Column(Integer, default=1, nullable=False, index=True) 
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    likes          = relationship("PostLikeRecord",     back_populates="post", cascade="all, delete-orphan")
    bookmarks      = relationship("PostBookmarkRecord", back_populates="post", cascade="all, delete-orphan")
    comments       = relationship("PostCommentRecord",  back_populates="post", cascade="all, delete-orphan")
    category_links = relationship("PostCategoryLink",   back_populates="post", cascade="all, delete-orphan")
    keyword_links  = relationship("PostKeywordLink",    back_populates="post", cascade="all, delete-orphan")
    reports        = relationship("PostReportRecord",   back_populates="post", cascade="all, delete-orphan")

class PostLikeRecord(PostsBase):
    __tablename__ = "post_likes"
    like_id   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id   = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    user_id   = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    post = relationship("PostRecord", back_populates="likes")
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_likes_post_user"),)


class PostBookmarkRecord(PostsBase):
    __tablename__ = "post_bookmarks"
    bookmark_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id     = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    user_id     = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    post = relationship("PostRecord", back_populates="bookmarks")
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_bookmarks_post_user"),)


class PostCommentRecord(PostsBase):
    __tablename__     = "post_comments"
    comment_id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id           = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    user_id           = Column(UUID(as_uuid=True), nullable=False, index=True)
    parent_comment_id = Column(UUID(as_uuid=True), ForeignKey("post_comments.comment_id", ondelete="CASCADE"), nullable=True)
    content           = Column(Text, nullable=False)
    is_edited         = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    post = relationship("PostRecord", back_populates="comments")


class CategoryRecord(PostsBase):
    __tablename__ = "categories"
    category_id   = Column(Integer, primary_key=True, autoincrement=True)
    slug          = Column(String(60), unique=True, index=True, nullable=False)
    label         = Column(String(80), nullable=False)
    usage_count   = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    post_links = relationship("PostCategoryLink", back_populates="category")


class PostCategoryLink(PostsBase):
    __tablename__ = "post_categories"
    post_id     = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.category_id", ondelete="CASCADE"), primary_key=True)
    post     = relationship("PostRecord",     back_populates="category_links")
    category = relationship("CategoryRecord", back_populates="post_links")


class KeywordRecord(PostsBase):
    __tablename__ = "keywords"
    keyword_id  = Column(Integer, primary_key=True, autoincrement=True)
    word        = Column(String(80), unique=True, index=True, nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    post_links  = relationship("PostKeywordLink", back_populates="keyword")


class PostKeywordLink(PostsBase):
    __tablename__ = "post_keywords"
    post_id    = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), primary_key=True)
    keyword_id = Column(Integer, ForeignKey("keywords.keyword_id", ondelete="CASCADE"), primary_key=True)
    post    = relationship("PostRecord",    back_populates="keyword_links")
    keyword = relationship("KeywordRecord", back_populates="post_links")


class PendingCategoryRecord(PostsBase):
    __tablename__         = "pending_categories"
    pending_id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label                 = Column(String(80), nullable=False)
    normalized_slug       = Column(String(60), nullable=False, index=True)
    suggested_by_user_id  = Column(UUID(as_uuid=True), nullable=False, index=True)
    status                = Column(String(12), default="pending", nullable=False, index=True)
    created_timestamp     = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_timestamp    = Column(DateTime(timezone=True), nullable=True)


class PostReportRecord(PostsBase):
    __tablename__      = "post_reports"
    report_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id            = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    reporter_user_id   = Column(UUID(as_uuid=True), nullable=False, index=True)
    reason             = Column(Text, nullable=False)
    status             = Column(String(20), default="open", nullable=False)
    created_timestamp  = Column(DateTime(timezone=True), server_default=func.now())
    post = relationship("PostRecord", back_populates="reports")


# ════════════════════════════════════════════════════════════════════
#  ANALYTICS DATABASE  (star schema warehouse)
# ════════════════════════════════════════════════════════════════════

class DimDate(AnalyticsBase):
    """One row per calendar day."""
    __tablename__ = "dim_date"
    date_key     = Column(Date, primary_key=True)
    year         = Column(Integer, nullable=False)
    quarter      = Column(Integer, nullable=False)
    month        = Column(Integer, nullable=False)
    day          = Column(Integer, nullable=False)
    day_of_week  = Column(Integer, nullable=False)   # 0=Mon .. 6=Sun
    week_of_year = Column(Integer, nullable=False)
    is_weekend   = Column(Integer, nullable=False, default=0)


class DimUser(AnalyticsBase):
    """Denormalised user label cache."""
    __tablename__          = "dim_user"
    user_id                = Column(UUID(as_uuid=True), primary_key=True)
    user_handle            = Column(String(50), nullable=False)
    role_name              = Column(String(50), nullable=True)
    first_seen_date        = Column(Date, nullable=True)
    last_synced_timestamp  = Column(DateTime(timezone=True), server_default=func.now())


class DimPost(AnalyticsBase):
    """Denormalised post label cache."""
    __tablename__          = "dim_post"
    post_id                = Column(UUID(as_uuid=True), primary_key=True)
    title                  = Column(String(255), nullable=True)
    publisher_user_id      = Column(UUID(as_uuid=True), nullable=True, index=True)
    publisher_handle       = Column(String(50), nullable=True)
    created_date           = Column(Date, nullable=True)
    last_synced_timestamp  = Column(DateTime(timezone=True), server_default=func.now())


class DimCategory(AnalyticsBase):
    __tablename__          = "dim_category"
    category_id            = Column(Integer, primary_key=True)
    slug                   = Column(String(60), nullable=False)
    label                  = Column(String(80), nullable=False)
    last_synced_timestamp  = Column(DateTime(timezone=True), server_default=func.now())


class FactDailySnapshot(AnalyticsBase):
    """Platform-wide daily totals. Grain: one row per day. UPSERT-safe."""
    __tablename__      = "fact_daily_snapshot"
    snapshot_date      = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)
    total_users        = Column(BigInteger, nullable=False, default=0)
    total_posts        = Column(BigInteger, nullable=False, default=0)
    total_likes        = Column(BigInteger, nullable=False, default=0)
    total_bookmarks    = Column(BigInteger, nullable=False, default=0)
    total_comments     = Column(BigInteger, nullable=False, default=0)
    total_follows      = Column(BigInteger, nullable=False, default=0)
    total_categories   = Column(BigInteger, nullable=False, default=0)
    total_keywords     = Column(BigInteger, nullable=False, default=0)
    open_reports       = Column(BigInteger, nullable=False, default=0)
    new_users          = Column(Integer,    nullable=False, default=0)
    new_posts          = Column(Integer,    nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("brin_fact_daily_snapshot_date", "snapshot_date", postgresql_using="brin"),
    )


class FactPostEngagement(AnalyticsBase):
    """Per-post engagement. Grain: (post_id, snapshot_date)."""
    __tablename__      = "fact_post_engagement"
    snapshot_date      = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)
    post_id            = Column(UUID(as_uuid=True), primary_key=True)
    likes              = Column(BigInteger, nullable=False, default=0)
    bookmarks          = Column(BigInteger, nullable=False, default=0)
    comments           = Column(BigInteger, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_fact_post_engagement_post",  "post_id"),
        Index("brin_fact_post_engagement_date", "snapshot_date", postgresql_using="brin"),
    )


class FactCategoryDaily(AnalyticsBase):
    """Per-category usage. Grain: (category_id, snapshot_date)."""
    __tablename__      = "fact_category_daily"
    snapshot_date      = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)
    category_id        = Column(Integer, primary_key=True)
    usage_count        = Column(BigInteger, nullable=False, default=0)
    post_count         = Column(BigInteger, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("brin_fact_category_daily_date", "snapshot_date", postgresql_using="brin"),
    )


class FactWeeklyDelta(AnalyticsBase):
    """This-week vs last-week new user/post counts. Grain: one row per snapshot_date."""
    __tablename__        = "fact_weekly_delta"
    snapshot_date        = Column(Date, primary_key=True)
    this_week_new_users  = Column(Integer, nullable=False, default=0)
    this_week_new_posts  = Column(Integer, nullable=False, default=0)
    last_week_new_users  = Column(Integer, nullable=False, default=0)
    last_week_new_posts  = Column(Integer, nullable=False, default=0)
    captured_timestamp   = Column(DateTime(timezone=True), server_default=func.now())


class FactTopPost(AnalyticsBase):
    """Ranked top posts per snapshot. Grain: (snapshot_date, rank_by, rank)."""
    __tablename__         = "fact_top_post"
    snapshot_date         = Column(Date, primary_key=True)
    rank_by               = Column(String(12), primary_key=True)   # likes|bookmarks|comments
    rank                  = Column(Integer, primary_key=True)
    post_id               = Column(UUID(as_uuid=True), nullable=False)
    title                 = Column(String(255), nullable=True)
    publisher_handle      = Column(String(50), nullable=True)
    total_likes_count     = Column(BigInteger, nullable=False, default=0)
    total_comments_count  = Column(BigInteger, nullable=False, default=0)
    metric_value          = Column(BigInteger, nullable=False, default=0)
    captured_timestamp    = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_fact_top_post_lookup", "snapshot_date", "rank_by", "rank"),
    )


class FactTopUser(AnalyticsBase):
    """Ranked top users per snapshot. Grain: (snapshot_date, rank_by, rank)."""
    __tablename__      = "fact_top_user"
    snapshot_date      = Column(Date, primary_key=True)
    rank_by            = Column(String(12), primary_key=True)   # followers|posts
    rank               = Column(Integer, primary_key=True)
    user_id            = Column(UUID(as_uuid=True), nullable=False)
    user_handle        = Column(String(50), nullable=True)
    role_name          = Column(String(50), nullable=True)
    follower_count     = Column(BigInteger, nullable=False, default=0)
    post_count         = Column(BigInteger, nullable=False, default=0)
    metric_value       = Column(BigInteger, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_fact_top_user_lookup", "snapshot_date", "rank_by", "rank"),
    )


class FactTopCommenter(AnalyticsBase):
    """Ranked top commenters per snapshot. Grain: (snapshot_date, rank)."""
    __tablename__      = "fact_top_commenter"
    snapshot_date      = Column(Date, primary_key=True)
    rank               = Column(Integer, primary_key=True)
    user_id            = Column(UUID(as_uuid=True), nullable=False)
    user_handle        = Column(String(50), nullable=True)
    comment_count      = Column(BigInteger, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_fact_top_commenter_lookup", "snapshot_date", "rank"),
    )


class EtlRunLog(AnalyticsBase):
    """One row per ETL run. Written by snapshot.py, read by /admin/analytics/etl-status."""
    __tablename__       = "etl_run_log"
    run_id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_name            = Column(String(80), nullable=False, index=True)
    snapshot_date       = Column(Date, nullable=True, index=True)
    status              = Column(String(20), nullable=False, default="running")
    rows_written        = Column(Integer, nullable=False, default=0)
    error_message       = Column(Text, nullable=True)
    started_timestamp   = Column(DateTime(timezone=True), server_default=func.now())
    finished_timestamp  = Column(DateTime(timezone=True), nullable=True)
    duration_seconds    = Column(Numeric(10, 3), nullable=True)


# ════════════════════════════════════════════════════════════════════
#  ADMIN DATABASE  (audit trail + operational data)
# ════════════════════════════════════════════════════════════════════

class AdminActionLog(AdminBase):
    """Every mutating control-panel action. Written synchronously after each mutation."""
    __tablename__       = "admin_action_log"
    action_id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id       = Column(UUID(as_uuid=True), nullable=False, index=True)
    admin_handle        = Column(String(50), nullable=True)
    action_type         = Column(String(60), nullable=False, index=True)
    target_type         = Column(String(40), nullable=True)
    target_id           = Column(String(64), nullable=True, index=True)
    payload             = Column(JSONB, default=dict, nullable=False)
    ip_address          = Column(String(64), nullable=True)
    occurred_timestamp  = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    __table_args__ = (
        Index("ix_admin_action_type_time", "action_type", "occurred_timestamp"),
    )


class RoleChangeHistory(AdminBase):
    """Full audit trail of every role assignment."""
    __tablename__          = "role_change_history"
    change_id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_user_id        = Column(UUID(as_uuid=True), nullable=False, index=True)
    changed_by_user_id     = Column(UUID(as_uuid=True), nullable=False, index=True)
    old_role               = Column(String(50), nullable=True)
    new_role               = Column(String(50), nullable=False)
    reason                 = Column(Text, nullable=True)
    occurred_timestamp     = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ReportResolution(AdminBase):
    """How a post report was dispositioned."""
    __tablename__          = "report_resolution"
    resolution_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id              = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id                = Column(UUID(as_uuid=True), nullable=True, index=True)
    resolved_by_user_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    resolution             = Column(String(30), nullable=False)
    notes                  = Column(Text, nullable=True)
    occurred_timestamp     = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminSetting(AdminBase):
    """Key/value settings toggled from the control panel."""
    __tablename__       = "admin_setting"
    setting_key         = Column(String(80), primary_key=True)
    setting_value       = Column(JSONB, default=dict, nullable=False)
    updated_by_user_id  = Column(UUID(as_uuid=True), nullable=True)
    updated_timestamp   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class SupportTicketRecord(UsersBase):
    __tablename__ = "support_tickets"
 
    ticket_id   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # nullable so logged-out visitors can still submit
    user_id     = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_email = Column(String(255), nullable=True)
    category      = Column(String(50),  nullable=False, index=True)
    subject       = Column(String(255), nullable=False)
    description   = Column(Text,        nullable=False)
    # open | in_progress | resolved | closed
    status        = Column(String(20),  default="open", nullable=False, index=True)
 
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
 
    user = relationship("PlatformUserRecord", backref="support_tickets")

# ════════════════════════════════════════════════════════════════════
#  AUDIT HELPERS  (moved from app/analytics/audit.py)
#  Import these directly: from app.db.models import log_admin_action
# ════════════════════════════════════════════════════════════════════

def log_admin_action(
    admin_db,
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
    admin_db,
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
    admin_db,
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