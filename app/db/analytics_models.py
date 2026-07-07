"""
app/db/analytics_models.py

Models for the two new databases:
  AnalyticsBase -> analytics_db  (star schema, append-only warehouse)
  AdminBase     -> admin_db      (audit trail + operational data)
"""

from sqlalchemy import (
    Column, String, DateTime, Integer, BigInteger, Text,
    Date, ForeignKey, Index, Numeric,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.db.database import AnalyticsBase, AdminBase


# ============================================================
#  ANALYTICS DATABASE — star schema
# ============================================================

class DimDate(AnalyticsBase):
    """One row per calendar day."""
    __tablename__ = "dim_date"
    date_key      = Column(Date, primary_key=True)
    year          = Column(Integer, nullable=False)
    quarter       = Column(Integer, nullable=False)
    month         = Column(Integer, nullable=False)
    day           = Column(Integer, nullable=False)
    day_of_week   = Column(Integer, nullable=False)   # 0=Mon .. 6=Sun
    week_of_year  = Column(Integer, nullable=False)
    is_weekend    = Column(Integer, nullable=False, default=0)


class DimUser(AnalyticsBase):
    """Denormalised user label cache for notebook joins."""
    __tablename__ = "dim_user"
    user_id              = Column(UUID(as_uuid=True), primary_key=True)
    user_handle          = Column(String(50), nullable=False)
    role_name            = Column(String(50), nullable=True)
    first_seen_date      = Column(Date, nullable=True)
    last_synced_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class DimPost(AnalyticsBase):
    """Denormalised post label cache."""
    __tablename__ = "dim_post"
    post_id              = Column(UUID(as_uuid=True), primary_key=True)
    title                = Column(String(255), nullable=True)
    publisher_user_id    = Column(UUID(as_uuid=True), nullable=True, index=True)
    publisher_handle     = Column(String(50), nullable=True)
    created_date         = Column(Date, nullable=True)
    last_synced_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class DimCategory(AnalyticsBase):
    __tablename__ = "dim_category"
    category_id           = Column(Integer, primary_key=True)
    slug                  = Column(String(60), nullable=False)
    label                 = Column(String(80), nullable=False)
    last_synced_timestamp = Column(DateTime(timezone=True), server_default=func.now())


class FactDailySnapshot(AnalyticsBase):
    """
    Platform-wide daily snapshot. Grain: one row per day.
    Append-only; UPSERT on snapshot_date makes re-runs idempotent.
    """
    __tablename__     = "fact_daily_snapshot"
    snapshot_date     = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)

    total_users       = Column(BigInteger, nullable=False, default=0)
    total_posts       = Column(BigInteger, nullable=False, default=0)
    total_likes       = Column(BigInteger, nullable=False, default=0)
    total_bookmarks   = Column(BigInteger, nullable=False, default=0)
    total_comments    = Column(BigInteger, nullable=False, default=0)
    total_follows     = Column(BigInteger, nullable=False, default=0)
    total_categories  = Column(BigInteger, nullable=False, default=0)
    total_keywords    = Column(BigInteger, nullable=False, default=0)
    open_reports      = Column(BigInteger, nullable=False, default=0)
    new_users         = Column(Integer,    nullable=False, default=0)
    new_posts         = Column(Integer,    nullable=False, default=0)

    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("brin_fact_daily_snapshot_date", "snapshot_date", postgresql_using="brin"),
    )


class FactPostEngagement(AnalyticsBase):
    """Per-post engagement snapshot. Grain: (post_id, snapshot_date)."""
    __tablename__  = "fact_post_engagement"
    snapshot_date  = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)
    post_id        = Column(UUID(as_uuid=True), primary_key=True)

    likes          = Column(BigInteger, nullable=False, default=0)
    bookmarks      = Column(BigInteger, nullable=False, default=0)
    comments       = Column(BigInteger, nullable=False, default=0)

    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_fact_post_engagement_post", "post_id"),
        Index("brin_fact_post_engagement_date", "snapshot_date", postgresql_using="brin"),
    )


class FactCategoryDaily(AnalyticsBase):
    """Per-category usage. Grain: (category_id, snapshot_date)."""
    __tablename__  = "fact_category_daily"
    snapshot_date  = Column(Date, ForeignKey("dim_date.date_key"), primary_key=True)
    category_id    = Column(Integer, primary_key=True)

    usage_count    = Column(BigInteger, nullable=False, default=0)
    post_count     = Column(BigInteger, nullable=False, default=0)

    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("brin_fact_category_daily_date", "snapshot_date", postgresql_using="brin"),
    )


class EtlRunLog(AnalyticsBase):
    """Bookkeeping row for every ETL / snapshot run."""
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


# ============================================================
#  ADMIN DATABASE — audit trail + operational data
# ============================================================

class AdminActionLog(AdminBase):
    """
    Every mutating admin action from the control panel.
    Written synchronously at action time — never stale.
    The payload field stores a before-snapshot of the deleted/changed object
    because the source row is gone after a delete.
    """
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
    """How a post report was dispositioned in the control panel."""
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
    __tablename__          = "admin_setting"
    setting_key            = Column(String(80), primary_key=True)
    setting_value          = Column(JSONB, default=dict, nullable=False)
    updated_by_user_id     = Column(UUID(as_uuid=True), nullable=True)
    updated_timestamp      = Column(DateTime(timezone=True),
                                    server_default=func.now(), onupdate=func.now())
    
class FactWeeklyDelta(AnalyticsBase):
    """
    Stores the this-week / last-week new-user & new-post counts as of a
    snapshot, so the stat-card trend arrows work from the DB.
    Grain: one row per snapshot_date.
    """
    __tablename__ = "fact_weekly_delta"
    snapshot_date = Column(Date, primary_key=True)
 
    this_week_new_users = Column(Integer, nullable=False, default=0)
    this_week_new_posts = Column(Integer, nullable=False, default=0)
    last_week_new_users = Column(Integer, nullable=False, default=0)
    last_week_new_posts = Column(Integer, nullable=False, default=0)
 
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
 
 
class FactTopPost(AnalyticsBase):
    """
    Ranked top posts as of a snapshot, for each ranking dimension.
    Grain: (snapshot_date, rank_by, rank).
    rank_by in {'likes','bookmarks','comments'}.
    """
    __tablename__ = "fact_top_post"
    snapshot_date = Column(Date, primary_key=True)
    rank_by       = Column(String(12), primary_key=True)
    rank          = Column(Integer, primary_key=True)   # 1..N
 
    post_id               = Column(UUID(as_uuid=True), nullable=False)
    title                 = Column(String(255), nullable=True)
    publisher_handle      = Column(String(50), nullable=True)
    total_likes_count     = Column(BigInteger, nullable=False, default=0)
    total_comments_count  = Column(BigInteger, nullable=False, default=0)
    metric_value          = Column(BigInteger, nullable=False, default=0)  # value of rank_by
 
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
 
    __table_args__ = (
        Index("ix_fact_top_post_lookup", "snapshot_date", "rank_by", "rank"),
    )
 
 
class FactTopUser(AnalyticsBase):
    """
    Ranked top users as of a snapshot, for each ranking dimension.
    Grain: (snapshot_date, rank_by, rank). rank_by in {'followers','posts'}.
    """
    __tablename__ = "fact_top_user"
    snapshot_date = Column(Date, primary_key=True)
    rank_by       = Column(String(12), primary_key=True)
    rank          = Column(Integer, primary_key=True)
 
    user_id        = Column(UUID(as_uuid=True), nullable=False)
    user_handle    = Column(String(50), nullable=True)
    role_name      = Column(String(50), nullable=True)
    follower_count = Column(BigInteger, nullable=False, default=0)
    post_count     = Column(BigInteger, nullable=False, default=0)
 
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
 
    __table_args__ = (
        Index("ix_fact_top_user_lookup", "snapshot_date", "rank_by", "rank"),
    )
 
 
class FactTopCommenter(AnalyticsBase):
    """
    Ranked most-active commenters as of a snapshot.
    Grain: (snapshot_date, rank).
    """
    __tablename__ = "fact_top_commenter"
    snapshot_date = Column(Date, primary_key=True)
    rank          = Column(Integer, primary_key=True)
 
    user_id       = Column(UUID(as_uuid=True), nullable=False)
    user_handle   = Column(String(50), nullable=True)
    comment_count = Column(BigInteger, nullable=False, default=0)
 
    captured_timestamp = Column(DateTime(timezone=True), server_default=func.now())
 
    __table_args__ = (
        Index("ix_fact_top_commenter_lookup", "snapshot_date", "rank"),
    )