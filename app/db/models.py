import uuid
from sqlalchemy import (
    Column, String, DateTime, Integer, Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from geoalchemy2 import Geometry

from app.db.database import UsersBase, PostsBase


# ============================================================
#  USERS DATABASE
# ============================================================

class RoleRecord(UsersBase):
    """Role-based access control. Permissions stored as JSON flags so new
    capabilities don't require a migration."""
    __tablename__ = "roles"

    role_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    permissions = Column(JSONB, default=dict, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    users = relationship("PlatformUserRecord", back_populates="role")


class PlatformUserRecord(UsersBase):
    __tablename__ = "platform_users"

    user_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_handle = Column(String(50), unique=True, index=True, nullable=False)
    email_address = Column(String(100), unique=True, index=True, nullable=False)
    hashed_security_password = Column(String(255), nullable=False)
    avatar_storage_url = Column(String)

    role_id = Column(Integer, ForeignKey("roles.role_id"), nullable=True, index=True)

    # Denormalized social counters (kept in sync via follow events / triggers)
    follower_count = Column(Integer, default=0, nullable=False)
    following_count = Column(Integer, default=0, nullable=False)
    publication_count = Column(Integer, default=0, nullable=False)

    is_active = Column(Integer, default=1, nullable=False)  # soft-disable flag
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    role = relationship("RoleRecord", back_populates="users")


class FollowEventRecord(UsersBase):
    """Append-only audit log of every follow/unfollow action.
    NEVER updated or deleted — current state is derived from the latest event
    per (actor, target) pair, or read from FollowCurrentRecord."""
    __tablename__ = "follow_events"

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)   # who acted
    target_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # whom
    action = Column(String(10), nullable=False)  # 'follow' | 'unfollow'
    occurred_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_follow_events_pair_time", "actor_user_id", "target_user_id", "occurred_timestamp"),
    )


class FollowCurrentRecord(UsersBase):
    """Materialized current-follow state — fast lookup, maintained from the
    event stream. One row per active follow relationship."""
    __tablename__ = "follows_current"

    actor_user_id = Column(UUID(as_uuid=True), primary_key=True)
    target_user_id = Column(UUID(as_uuid=True), primary_key=True)
    followed_since = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_follows_current_target", "target_user_id"),
    )


# ============================================================
#  POSTS DATABASE  (PostGIS)
# ============================================================

class GeographicPublicationRecord(PostsBase):
    __tablename__ = "geographic_publications"

    publication_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Cross-database link — enforced logically in the API, not by an FK,
    # because users live in a separate Postgres instance.
    publisher_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    publication_title = Column(String(255), nullable=False)
    spatial_geometry = Column(
        Geometry(geometry_type="GEOMETRY", srid=4326), nullable=False
    )
    layer_attribute_metadata = Column(JSONB, default=dict)

    # URL-safe public share slug (e.g. short nanoid). Default share link = /p/{slug}
    share_slug = Column(String(32), unique=True, index=True, nullable=False)

    # Denormalized engagement counters (source of truth lives here; Redis mirrors)
    total_likes_count = Column(Integer, default=0, nullable=False)
    total_comments_count = Column(Integer, default=0, nullable=False)

    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    likes = relationship("PublicationLikeRecord", back_populates="publication",
                         cascade="all, delete-orphan")
    comments = relationship("PublicationCommentRecord", back_populates="publication",
                            cascade="all, delete-orphan")
    category_links = relationship("PublicationCategoryLink", back_populates="publication",
                                  cascade="all, delete-orphan")


class PublicationLikeRecord(PostsBase):
    __tablename__ = "publication_likes"

    like_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id = Column(
        UUID(as_uuid=True),
        ForeignKey("geographic_publications.publication_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # cross-db, logical link
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    publication = relationship("GeographicPublicationRecord", back_populates="likes")

    __table_args__ = (
        # A user can like a publication at most once
        UniqueConstraint("publication_id", "user_id", name="uq_like_user_publication"),
    )


class PublicationCommentRecord(PostsBase):
    __tablename__ = "publication_comments"

    comment_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id = Column(
        UUID(as_uuid=True),
        ForeignKey("geographic_publications.publication_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # publisher of the comment

    # Self-referencing FK for nested replies (adjacency list).
    # NULL = top-level comment; otherwise points at the comment being replied to.
    parent_comment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("publication_comments.comment_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    content = Column(Text, nullable=False)
    is_edited = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    publication = relationship("GeographicPublicationRecord", back_populates="comments")
    replies = relationship(
        "PublicationCommentRecord",
        backref="parent",
        remote_side=[comment_id],
    )


class CategoryRecord(PostsBase):
    """Approved, canonical keyword list. Publications reference these via the
    join table instead of storing string arrays — indexed integer joins are
    far cheaper than scanning text arrays."""
    __tablename__ = "categories"

    category_id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(60), unique=True, index=True, nullable=False)
    label = Column(String(80), nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    publication_links = relationship("PublicationCategoryLink", back_populates="category")


class PublicationCategoryLink(PostsBase):
    """Many-to-many join between publications and approved categories."""
    __tablename__ = "publication_categories"

    publication_id = Column(
        UUID(as_uuid=True),
        ForeignKey("geographic_publications.publication_id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id = Column(
        Integer,
        ForeignKey("categories.category_id", ondelete="CASCADE"),
        primary_key=True,
    )

    publication = relationship("GeographicPublicationRecord", back_populates="category_links")
    category = relationship("CategoryRecord", back_populates="publication_links")


class PendingTagRecord(PostsBase):
    """Holding pen for user-suggested keywords awaiting manual approval.
    Approving a row inserts into `categories` and flips status to 'approved'."""
    __tablename__ = "pending_tags"

    pending_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label = Column(String(80), nullable=False)
    normalized_slug = Column(String(60), nullable=False, index=True)
    suggested_by_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(12), default="pending", nullable=False, index=True)  # pending|approved|rejected
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_timestamp = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("normalized_slug", "status", name="uq_pending_slug_status"),
    )