"""
app/db/models.py
================
Single source of truth for every SQLAlchemy model.

Databases:
  UsersBase    → users_db      (users, roles, follows, organisations + members, consent records)
  PostsBase    → posts_db      (posts, categories, keywords, likes, comments, views, reports, post shares)
  AnalyticsBase→ analytics_db  (daily aggregates for the admin growth charts + their run log)
  AdminBase    → admin_db      (audit trail, role history, report resolutions)
  DatasetsBase → datasets_db   (dataset catalog + dataset shares; data itself in gisviz.duckdb)
  MiscBase     → misc_db       (shared reference data: regions, ...)

"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import (
    Column, String, DateTime, Integer, BigInteger, Text,
    Date, ForeignKey, Index, Numeric, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import UsersBase, PostsBase, AnalyticsBase, AdminBase, DatasetsBase, MiscBase


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
    nickname = Column(String(40), nullable=True)
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
    token_version   = Column(Integer, default=0, server_default="0", nullable=False)
    account_type    = Column(String(16), default="individual", server_default="individual", nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    role     = relationship("RoleRecord", back_populates="users")
    location = relationship("UserLocationRecord", back_populates="user", uselist=False, cascade="all, delete-orphan")


class OrganizationRecord(UsersBase):
    __tablename__ = "organizations"
    org_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_user_id = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"),
                             unique=True, nullable=False)
    name            = Column(String(160), nullable=False)
    email_domain    = Column(String(190), unique=True, nullable=False, index=True)
    status          = Column(String(12), default="pending", server_default="pending", nullable=False, index=True)
    reviewed_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    reviewed_timestamp  = Column(DateTime(timezone=True), nullable=True)
    created_timestamp   = Column(DateTime(timezone=True), server_default=func.now())


class OrganizationMemberRecord(UsersBase):
    __tablename__ = "organization_members"
    org_id    = Column(UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"), primary_key=True)
    user_id   = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), primary_key=True, index=True)
    role      = Column(String(12), default="member", nullable=False)
    source    = Column(String(12), default="domain", nullable=False)
    joined_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class ConsentRecord(UsersBase):
    __tablename__ = "consent_records"
    consent_id  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    document    = Column(String(20), nullable=False)
    version     = Column(String(20), nullable=False)
    accepted_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class RevokedTokenRecord(UsersBase):
    __tablename__ = "revoked_tokens"
    jti        = Column(String(32), primary_key=True)
    user_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), server_default=func.now())


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

class DatasetRecord(DatasetsBase):
    __tablename__ = "datasets"
    dataset_id    = Column(String(64), primary_key=True)
    title         = Column(String(255), nullable=False)
    description   = Column(Text, nullable=True)
    category      = Column(String(80), nullable=True, index=True)
    region        = Column(String(60), nullable=True, index=True)
    format        = Column(String(20), nullable=True)
    source_name   = Column(String(255), nullable=True)
    source_url    = Column(String(1024), nullable=True)
    license       = Column(String(80), nullable=True)
    publisher     = Column(String(120), nullable=True)
    geometry_type = Column(String(20), nullable=True)
    row_count     = Column(BigInteger, default=0, nullable=False)
    crs           = Column(String(32), nullable=True)
    bbox          = Column(JSONB, nullable=True)
    columns       = Column(JSONB, nullable=True)
    keywords      = Column(Text, nullable=True)
    data_version  = Column(String(32), nullable=True)
    data_uploaded_at = Column(DateTime(timezone=True), nullable=True)
    file_size_bytes = Column(BigInteger, nullable=True)
    show_data     = Column(Integer, default=0, server_default="0", nullable=False)
    status        = Column(String(12), default="inactive", nullable=False, index=True)
    visibility    = Column(String(10), default="public", server_default="public", nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    pipeline      = Column(String(10), default="batch", server_default="batch", nullable=False, index=True)
    refresh_seconds = Column(Integer, default=60, server_default="60", nullable=False)
    retention_rows  = Column(Integer, nullable=True)
    ingest_key_hash = Column(String(64), nullable=True)
    last_ingested_at = Column(DateTime(timezone=True), nullable=True)
    license_code  = Column(String(40), nullable=True)
    attribution   = Column(Text, nullable=True)
    copyright_holder = Column(String(255), nullable=True)
    contains_personal_data = Column(Integer, default=0, server_default="0", nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DatasetShareRecord(DatasetsBase):
    __tablename__ = "dataset_shares"
    share_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id    = Column(String(64), ForeignKey("datasets.dataset_id", ondelete="CASCADE"), nullable=False, index=True)
    grantee_type  = Column(String(8), nullable=False)
    grantee_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    permission    = Column(String(10), default="view", server_default="view", nullable=False)
    granted_by_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_timestamp  = Column(DateTime(timezone=True), server_default=func.now())
    revoked_timestamp  = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    __table_args__ = (
        Index("uq_dataset_shares_active", "dataset_id", "grantee_type", "grantee_id", unique=True,
              postgresql_where=text("revoked_timestamp IS NULL")),
    )


class PostRecord(PostsBase):
    __tablename__   = "posts"
    post_id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publisher_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    title           = Column(String(255), nullable=False)
    description     = Column(Text, nullable=True)
    
    # Legacy flat spec storage (can be phased out or used alongside visual_config)
    visual_spec     = Column(JSONB, nullable=True)
    backdrop        = Column(JSONB, nullable=True)
    visual_image_path = Column(String, nullable=True)
    
    # New Structured Editor Format
    visual_config   = Column(JSONB, nullable=False, default={})
    thumbnail_url   = Column(String, nullable=True)
    
    dataset_id      = Column(String(64), nullable=True, index=True)
    theme_color     = Column(String(7), nullable=True)
    note            = Column(Text, nullable=True)
    source_name     = Column(String(255), nullable=True)
    source_url      = Column(String(1024), nullable=True)
    share_slug      = Column(String(32), unique=True, index=True, nullable=False)
    total_likes_count    = Column(Integer, default=0, nullable=False)
    total_comments_count = Column(Integer, default=0, nullable=False)
    views_count     = Column(Integer, default=0, server_default="0", nullable=False, index=True)
    region          = Column(String(60), nullable=True, index=True)
    visibility      = Column(String(10), default="public", server_default="public", nullable=False, index=True)
    post_type       = Column(String(10), default="standard", server_default="standard", nullable=False)
    is_active       = Column(Integer, default=1, nullable=False, index=True) 
    deactivated_by_dataset = Column(Integer, default=0, server_default="0", nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    likes          = relationship("PostLikeRecord",     back_populates="post", cascade="all, delete-orphan")
    bookmarks      = relationship("PostBookmarkRecord", back_populates="post", cascade="all, delete-orphan")
    comments       = relationship("PostCommentRecord",  back_populates="post", cascade="all, delete-orphan")
    category_links = relationship("PostCategoryLink",   back_populates="post", cascade="all, delete-orphan")
    keyword_links  = relationship("PostKeywordLink",    back_populates="post", cascade="all, delete-orphan")
    reports        = relationship("PostReportRecord",   back_populates="post", cascade="all, delete-orphan")
    shares         = relationship("PostShareRecord",    back_populates="post", cascade="all, delete-orphan")


class PostShareRecord(PostsBase):
    __tablename__ = "post_shares"
    share_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id       = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    grantee_type  = Column(String(8), nullable=False)
    grantee_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    permission    = Column(String(10), default="view", server_default="view", nullable=False)
    granted_by_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_timestamp  = Column(DateTime(timezone=True), server_default=func.now())
    revoked_timestamp  = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    post = relationship("PostRecord", back_populates="shares")
    __table_args__ = (
        Index("uq_post_shares_active", "post_id", "grantee_type", "grantee_id", unique=True,
              postgresql_where=text("revoked_timestamp IS NULL")),
    )


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


class PostViewRecord(PostsBase):
    __tablename__    = "post_views"
    view_id          = Column(BigInteger, primary_key=True, autoincrement=True)
    post_id          = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False)
    user_id          = Column(UUID(as_uuid=True), nullable=True, index=True)
    viewer_hash      = Column(String(64), nullable=False)
    viewed_timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        Index("ix_post_views_post_time", "post_id", "viewed_timestamp"),
        Index("brin_post_views_time", "viewed_timestamp", postgresql_using="brin"),
    )


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
    description   = Column(Text, nullable=True)
    theme_color   = Column(String(7), nullable=True)
    sort_order    = Column(Integer, nullable=False, default=0, server_default="0")
    banner        = Column(JSONB, nullable=True)
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
#  ANALYTICS DATABASE  (daily history for the admin growth charts)
# ════════════════════════════════════════════════════════════════════

class DailyPlatformStats(AnalyticsBase):
    __tablename__   = "daily_platform_stats"
    stat_date       = Column(Date, primary_key=True)
    users           = Column(BigInteger, nullable=False, default=0)
    posts           = Column(BigInteger, nullable=False, default=0)
    likes           = Column(BigInteger, nullable=False, default=0)
    bookmarks       = Column(BigInteger, nullable=False, default=0)
    comments        = Column(BigInteger, nullable=False, default=0)
    follows         = Column(BigInteger, nullable=False, default=0)
    open_reports    = Column(BigInteger, nullable=False, default=0)
    new_users       = Column(Integer, nullable=False, default=0)
    new_posts       = Column(Integer, nullable=False, default=0)
    views           = Column(Integer, nullable=False, default=0)
    viewers         = Column(Integer, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DailyCategoryStats(AnalyticsBase):
    __tablename__   = "daily_category_stats"
    stat_date       = Column(Date, primary_key=True)
    category_id     = Column(Integer, primary_key=True)
    label           = Column(String(80), nullable=False)
    posts           = Column(BigInteger, nullable=False, default=0)
    new_posts       = Column(Integer, nullable=False, default=0)
    views           = Column(Integer, nullable=False, default=0)
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SnapshotRun(AnalyticsBase):
    __tablename__       = "snapshot_runs"
    run_id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger             = Column(String(12), nullable=False, default="schedule")
    stat_date           = Column(Date, nullable=True, index=True)
    status              = Column(String(12), nullable=False, default="running")
    rows_written        = Column(Integer, nullable=False, default=0)
    error_message       = Column(Text, nullable=True)
    started_timestamp   = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_timestamp  = Column(DateTime(timezone=True), nullable=True)
    duration_seconds    = Column(Numeric(10, 3), nullable=True)


OBSOLETE_ANALYTICS_TABLES = (
    "fact_post_view", "fact_daily_snapshot", "fact_post_engagement", "fact_category_daily",
    "fact_weekly_delta", "fact_top_post", "fact_top_user", "fact_top_commenter",
    "dim_user", "dim_post", "dim_category", "dim_date", "etl_run_log",
)


# ════════════════════════════════════════════════════════════════════
#  ADMIN DATABASE  (audit trail + operational data)
# ════════════════════════════════════════════════════════════════════

class AdminActionLog(AdminBase):
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
    __tablename__          = "role_change_history"
    change_id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_user_id        = Column(UUID(as_uuid=True), nullable=False, index=True)
    changed_by_user_id     = Column(UUID(as_uuid=True), nullable=False, index=True)
    old_role               = Column(String(50), nullable=True)
    new_role               = Column(String(50), nullable=False)
    reason                 = Column(Text, nullable=True)
    occurred_timestamp     = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ReportResolution(AdminBase):
    __tablename__          = "report_resolution"
    resolution_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id              = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id                = Column(UUID(as_uuid=True), nullable=True, index=True)
    resolved_by_user_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    resolution             = Column(String(30), nullable=False)
    notes                  = Column(Text, nullable=True)
    occurred_timestamp     = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminSetting(AdminBase):
    __tablename__       = "admin_setting"
    setting_key         = Column(String(80), primary_key=True)
    setting_value       = Column(JSONB, default=dict, nullable=False)
    updated_by_user_id  = Column(UUID(as_uuid=True), nullable=True)
    updated_timestamp   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class SupportTicketRecord(UsersBase):
    __tablename__ = "support_tickets"
 
    ticket_id   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

# ── MISC database (reference data shared across features) ────────────────────

class RegionRecord(MiscBase):
    __tablename__ = "regions"
    region_id   = Column(Integer, primary_key=True, autoincrement=True)
    code        = Column(String(60), unique=True, nullable=False, index=True)
    name        = Column(String(120), nullable=False)
    kind        = Column(String(20), nullable=False, default="region")
    parent_code = Column(String(60), nullable=True, index=True)
    iso_a2      = Column(String(2), nullable=True)
    iso_a3      = Column(String(3), nullable=True)
    m49         = Column(String(3), nullable=True)
    bbox        = Column(JSONB, nullable=True)
    sort_order  = Column(Integer, nullable=False, default=0)
    is_active   = Column(Integer, nullable=False, default=1)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VisualCategoryRecord(MiscBase):
    __tablename__ = "visual_categories"
    code        = Column(String(40), primary_key=True)
    name        = Column(String(80), nullable=False)
    color       = Column(String(7), nullable=True)
    question    = Column(Text, nullable=True)
    guide_goals = Column(JSONB, nullable=True)
    sort_order  = Column(Integer, nullable=False, default=0)


class VisualTypeRecord(MiscBase):
    __tablename__ = "visual_types"
    code         = Column(String(40), primary_key=True)
    name         = Column(String(120), nullable=False)
    category     = Column(String(40), nullable=False, index=True)
    kind         = Column(String(10), nullable=False)
    roles        = Column(JSONB, nullable=False)
    best_when    = Column(Text, nullable=True)
    avoid_when   = Column(Text, nullable=True)
    status       = Column(String(10), nullable=False, index=True)
    interactions = Column(JSONB, nullable=True)
    legacy_code  = Column(String(40), nullable=True)
    enabled      = Column(Integer, nullable=False, default=0, server_default="0")
    sort_order   = Column(Integer, nullable=False, default=0)
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AiPromptRecord(MiscBase):
    __tablename__ = "ai_prompts"
    code        = Column(String(60), primary_key=True)
    template    = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())