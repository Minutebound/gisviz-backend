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
    # bumped on password change/reset or "log out everywhere": every older token stops working
    token_version   = Column(Integer, default=0, server_default="0", nullable=False)
    # individual | organization (an organisation's own account; its members are users with its verified email domain)
    account_type    = Column(String(16), default="individual", server_default="individual", nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    role     = relationship("RoleRecord", back_populates="users")
    location = relationship("UserLocationRecord", back_populates="user", uselist=False, cascade="all, delete-orphan")


class OrganizationRecord(UsersBase):
    """An organisation on GisViz. Its profile is an account (account_user_id, account_type=organization) whose
    email domain was checked by an admin. Every user whose verified email is on that domain is a member
    (organization_members, source=domain) and sees what is shared with the organisation."""
    __tablename__ = "organizations"
    org_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_user_id = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"),
                             unique=True, nullable=False)
    name            = Column(String(160), nullable=False)
    email_domain    = Column(String(190), unique=True, nullable=False, index=True)    # lowercase, e.g. acme.org
    status          = Column(String(12), default="pending", server_default="pending", nullable=False, index=True)  # pending | verified | rejected
    reviewed_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    reviewed_timestamp  = Column(DateTime(timezone=True), nullable=True)
    created_timestamp   = Column(DateTime(timezone=True), server_default=func.now())


class OrganizationMemberRecord(UsersBase):
    __tablename__ = "organization_members"
    org_id    = Column(UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"), primary_key=True)
    user_id   = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), primary_key=True, index=True)
    role      = Column(String(12), default="member", nullable=False)       # owner (the org account) | member
    source    = Column(String(12), default="domain", nullable=False)       # account | domain
    joined_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class ConsentRecord(UsersBase):
    """GDPR: proof of what a user accepted (terms of service, privacy policy) and when."""
    __tablename__ = "consent_records"
    consent_id  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    document    = Column(String(20), nullable=False)          # terms | privacy
    version     = Column(String(20), nullable=False)
    accepted_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class RevokedTokenRecord(UsersBase):
    """A session token ended by logout. Tokens have no expiry, so a logged-out token is listed here
    (by its jti) and rejected from then on."""
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
    """
    Catalog row for one dataset, in the DATASETS database. Admins create it on /admin/datasets
    (dataset_id is a generated code: ds_00001, ds_00002, ...), upload its Parquet file (loaded as
    table <dataset_id> in gisviz.duckdb by the DuckDB service; the profile fields below are filled
    and it becomes active),
    activate/deactivate, edit and delete it. Posts reference it by dataset_id (different database,
    so the link is checked in code).
    """
    __tablename__ = "datasets"
    dataset_id    = Column(String(64), primary_key=True)          # code ds_00001 == data file name
    title         = Column(String(255), nullable=False)
    description   = Column(Text, nullable=True)
    category      = Column(String(80), nullable=True, index=True)   # categories.slug (posts DB)
    region        = Column(String(60), nullable=True, index=True)   # regions.code (misc DB)
    format        = Column(String(20), nullable=True)             # format of the last upload
    source_name   = Column(String(255), nullable=True)
    source_url    = Column(String(1024), nullable=True)
    license       = Column(String(80), nullable=True)
    publisher     = Column(String(120), nullable=True)
    geometry_type = Column(String(20), nullable=True)
    row_count     = Column(BigInteger, default=0, nullable=False)
    crs           = Column(String(32), nullable=True)
    bbox          = Column(JSONB, nullable=True)
    columns       = Column(JSONB, nullable=True)                  # column profile used by the chart suggester
    keywords      = Column(Text, nullable=True)                   # lowercase search text (id, title, columns...)
    data_version  = Column(String(32), nullable=True)             # fingerprint of the uploaded data file
    data_uploaded_at = Column(DateTime(timezone=True), nullable=True)   # when the file was last uploaded
    file_size_bytes = Column(BigInteger, nullable=True)            # size of the last uploaded data file
    show_data     = Column(Integer, default=0, server_default="0", nullable=False)  # 1 = rows shown on post pages (public)
    status        = Column(String(12), default="inactive", nullable=False, index=True)  # inactive | active
    # who may see it: public | private (owner, admins, people / organisations it is shared with)
    visibility    = Column(String(10), default="public", server_default="public", nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)        # a user or an organisation account
    # batch: refreshed now and then by uploading a file;  stream: rows pushed continuously (live posts)
    pipeline      = Column(String(10), default="batch", server_default="batch", nullable=False, index=True)
    refresh_seconds = Column(Integer, default=60, server_default="60", nullable=False)   # live posts re-read every n s
    retention_rows  = Column(Integer, nullable=True)                              # stream: keep the newest n rows
    ingest_key_hash = Column(String(64), nullable=True)                           # sha256 of the stream ingest key
    last_ingested_at = Column(DateTime(timezone=True), nullable=True)
    # copyright / licensing (app/services/licences.py)
    license_code  = Column(String(40), nullable=True)
    attribution   = Column(Text, nullable=True)                                   # e.g. "© OpenStreetMap contributors"
    copyright_holder = Column(String(255), nullable=True)
    contains_personal_data = Column(Integer, default=0, server_default="0", nullable=False)   # GDPR: rows never public
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DatasetShareRecord(DatasetsBase):
    """Who a private dataset is shared with, and who shared it (kept after revoking: the access history)."""
    __tablename__ = "dataset_shares"
    share_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id    = Column(String(64), ForeignKey("datasets.dataset_id", ondelete="CASCADE"), nullable=False, index=True)
    grantee_type  = Column(String(8), nullable=False)                       # user | org
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
    visual_image_path = Column(String, nullable=True)
    dataset_id      = Column(String(64), nullable=True, index=True)  # dataset behind the visual (datasets DB)
    visual_spec     = Column(JSONB, nullable=True)                     # spec rendered by <InteractiveVisual>
    theme_color     = Column(String(7), nullable=True)                 # post theme colour, e.g. #06ba24 (also stored in visual_spec.accent)
    note            = Column(Text, nullable=True)
    source_name     = Column(String(255), nullable=True)
    source_url      = Column(String(1024), nullable=True)
    share_slug      = Column(String(32), unique=True, index=True, nullable=False)
    total_likes_count    = Column(Integer, default=0, nullable=False)
    total_comments_count = Column(Integer, default=0, nullable=False)
    backdrop        = Column(JSONB, nullable=True)   # poster design: {mode, bg, texture, font, title_size, title_align, map_base, layout, dark}
    views_count     = Column(Integer, default=0, server_default="0", nullable=False, index=True)  # total page views (rows in post_views)
    region          = Column(String(60), nullable=True, index=True)   # regions.code (misc DB); required for new posts
    visibility      = Column(String(10), default="public", server_default="public", nullable=False, index=True)  # public | private
    post_type       = Column(String(10), default="standard", server_default="standard", nullable=False)       # standard | live
    is_active       = Column(Integer, default=1, nullable=False, index=True) 
    # 1 when the post was switched off because its dataset became inactive (it comes back with the dataset)
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
    """Who a private post is shared with, and who shared it (kept after revoking: the access history)."""
    __tablename__ = "post_shares"
    share_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id       = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    grantee_type  = Column(String(8), nullable=False)                       # user | org
    grantee_id    = Column(UUID(as_uuid=True), nullable=False, index=True)  # user_id or org_id (users DB)
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
    """One row per post page opened (POST /posts/{id}/view). posts.views_count is the running total.
    viewer_hash: SHA-256 of the user id (signed in) or IP + user agent (anonymous) with the server
    secret, never the raw IP; it lets the stats count distinct viewers. user_id is null for anonymous
    visitors and is cleared when an account is erased."""
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
    theme_color   = Column(String(7), nullable=True)          # #rrggbb, used as the post/visual accent
    sort_order    = Column(Integer, nullable=False, default=0, server_default="0")
    # the feed banner's animated scene, picked from the category's words by the poster topic engine
    # (app/services/banner_service.py): {"version", "topic", "scene", "color", "source", "generated_at"}
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
#  Written by app/analytics/snapshot.py: automatically every hour (today's row is
#  refreshed in place) and by the admin "Refresh" button. Only aggregates live here;
#  the raw events (views, likes, ...) stay in their own databases.
# ════════════════════════════════════════════════════════════════════

class DailyPlatformStats(AnalyticsBase):
    """One row per day: platform totals at the end of the day (today: at the last run) and what
    happened that day. Totals for days before the first run are rebuilt from creation dates."""
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
    views           = Column(Integer, nullable=False, default=0)    # post views that day
    viewers         = Column(Integer, nullable=False, default=0)    # distinct viewers that day
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DailyCategoryStats(AnalyticsBase):
    """One row per category per day. The label is copied in, so a renamed or deleted category
    still reads correctly in old rows."""
    __tablename__   = "daily_category_stats"
    stat_date       = Column(Date, primary_key=True)
    category_id     = Column(Integer, primary_key=True)
    label           = Column(String(80), nullable=False)
    posts           = Column(BigInteger, nullable=False, default=0)   # posts in the category (total)
    new_posts       = Column(Integer, nullable=False, default=0)
    views           = Column(Integer, nullable=False, default=0)      # views that day on its posts
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SnapshotRun(AnalyticsBase):
    """One row per run of the snapshot job (kept 60 days): proves it runs, and shows errors."""
    __tablename__       = "snapshot_runs"
    run_id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger             = Column(String(12), nullable=False, default="schedule")   # schedule|manual|backfill
    stat_date           = Column(Date, nullable=True, index=True)
    status              = Column(String(12), nullable=False, default="running")    # running|success|failed
    rows_written        = Column(Integer, nullable=False, default=0)
    error_message       = Column(Text, nullable=True)
    started_timestamp   = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_timestamp  = Column(DateTime(timezone=True), nullable=True)
    duration_seconds    = Column(Numeric(10, 3), nullable=True)


# Tables of the old star-schema warehouse, dropped at start-up (app/main.py). fact_post_view's rows are
# first copied into post_views (posts DB); the rest were snapshots nothing reads.
OBSOLETE_ANALYTICS_TABLES = (
    "fact_post_view", "fact_daily_snapshot", "fact_post_engagement", "fact_category_daily",
    "fact_weekly_delta", "fact_top_post", "fact_top_user", "fact_top_commenter",
    "dim_user", "dim_post", "dim_category", "dim_date", "etl_run_log",
)


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

# ── MISC database (reference data shared across features) ────────────────────

class RegionRecord(MiscBase):
    """Regions a dataset (and later a post, a user, a filter) can be tagged with.
    code is the stable key stored elsewhere (datasets.region); parent_code builds the hierarchy
    global > continent > sub-region > country."""
    __tablename__ = "regions"
    region_id   = Column(Integer, primary_key=True, autoincrement=True)
    code        = Column(String(60), unique=True, nullable=False, index=True)     # e.g. global, africa, eastern-africa, ke
    name        = Column(String(120), nullable=False)
    kind        = Column(String(20), nullable=False, default="region")            # global | continent | subregion | country | custom
    parent_code = Column(String(60), nullable=True, index=True)
    iso_a2      = Column(String(2), nullable=True)
    iso_a3      = Column(String(3), nullable=True)
    m49         = Column(String(3), nullable=True)                                # UN M49 area code
    bbox        = Column(JSONB, nullable=True)                                    # [minx, miny, maxx, maxy]
    sort_order  = Column(Integer, nullable=False, default=0)
    is_active   = Column(Integer, nullable=False, default=1)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VisualCategoryRecord(MiscBase):
    """The 7 families of visuals (data-to-viz): distribution, correlation, ranking, part_of_whole,
    evolution, map, flow. Seeded by GET /seed (app/main.py)."""
    __tablename__ = "visual_categories"
    code        = Column(String(40), primary_key=True)
    name        = Column(String(80), nullable=False)
    color       = Column(String(7), nullable=True)
    question    = Column(Text, nullable=True)               # what this family answers
    guide_goals = Column(JSONB, nullable=True)              # matching "data goals" of the Datylon guide
    sort_order  = Column(Integer, nullable=False, default=0)


class VisualTypeRecord(MiscBase):
    """One chart or map type. status: live (renders today) | next (small renderer addition) |
    later (new data shape / renderer). Only enabled types are offered to publishers and the AI."""
    __tablename__ = "visual_types"
    code         = Column(String(40), primary_key=True)
    name         = Column(String(120), nullable=False)
    category     = Column(String(40), nullable=False, index=True)   # visual_categories.code
    kind         = Column(String(10), nullable=False)               # chart | map
    roles        = Column(JSONB, nullable=False)                    # {"x": "categorical", "y": "numeric", "z?": ...}
    best_when    = Column(Text, nullable=True)
    avoid_when   = Column(Text, nullable=True)
    status       = Column(String(10), nullable=False, index=True)   # live | next | later
    interactions = Column(JSONB, nullable=True)                     # ["tooltip", "label_filter", "time_play", ...]
    legacy_code  = Column(String(40), nullable=True)                # code used by the current renderer, if different
    enabled      = Column(Integer, nullable=False, default=0, server_default="0")   # admin switch; seeded = live
    sort_order   = Column(Integer, nullable=False, default=0)
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AiPromptRecord(MiscBase):
    """Prompt templates used by AI features (e.g. visual_selection). Editable without a deploy."""
    __tablename__ = "ai_prompts"
    code        = Column(String(60), primary_key=True)
    template    = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    updated_timestamp = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())