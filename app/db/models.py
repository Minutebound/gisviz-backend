import uuid
from sqlalchemy import (
    Column, String, DateTime, Integer, Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import UsersBase, PostsBase

# ============================================================
#  USERS DATABASE
# ============================================================

class RoleRecord(UsersBase):
    """Role-based access control. 
    Expected roles: admin, editor, viewer, publisher, support."""
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
    
    # --- AUTHENTICATION & SECURITY ---
    is_verified = Column(Integer, default=0, nullable=False)
    verification_otp = Column(String(6), nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    
    password_reset_token = Column(String(128), unique=True, index=True, nullable=True)
    reset_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # --- PROFILE & SOCIALS ---
    avatar_path = Column(String, nullable=True)
    title = Column(String(100), nullable=True)
    linkedin_url = Column(String(255), nullable=True)
    medium_url = Column(String(255), nullable=True)
    website_url = Column(String(255), nullable=True)

    role_id = Column(Integer, ForeignKey("roles.role_id"), nullable=True, index=True)

    # --- COUNTERS ---
    follower_count = Column(Integer, default=0, nullable=False)
    following_count = Column(Integer, default=0, nullable=False)
    post_count = Column(Integer, default=0, nullable=False)

    is_active = Column(Integer, default=1, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    role = relationship("RoleRecord", back_populates="users")
    location = relationship("UserLocationRecord", back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserLocationRecord(UsersBase):
    __tablename__ = "user_locations"

    location_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("platform_users.user_id", ondelete="CASCADE"), unique=True)
    
    place = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    formatted_string = Column(String(255), nullable=True) 

    user = relationship("PlatformUserRecord", back_populates="location")


class FollowEventRecord(UsersBase):
    __tablename__ = "follow_events"

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(10), nullable=False)
    occurred_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_follow_events_pair_time", "actor_user_id", "target_user_id", "occurred_timestamp"),
    )


class FollowCurrentRecord(UsersBase):
    __tablename__ = "follows_current"

    actor_user_id = Column(UUID(as_uuid=True), primary_key=True)
    target_user_id = Column(UUID(as_uuid=True), primary_key=True)
    followed_since = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_follows_current_target", "target_user_id"),
    )


# ============================================================
#  POSTS DATABASE  
# ============================================================

class PostRecord(PostsBase):
    __tablename__ = "posts"

    post_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publisher_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True) 
    visual_image_path = Column(String, nullable=True) 

    share_slug = Column(String(32), unique=True, index=True, nullable=False)

    total_likes_count = Column(Integer, default=0, nullable=False)
    total_comments_count = Column(Integer, default=0, nullable=False)

    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    likes = relationship("PostLikeRecord", back_populates="post", cascade="all, delete-orphan")
    comments = relationship("PostCommentRecord", back_populates="post", cascade="all, delete-orphan")
    category_links = relationship("PostCategoryLink", back_populates="post", cascade="all, delete-orphan")
    keyword_links = relationship("PostKeywordLink", back_populates="post", cascade="all, delete-orphan")
    reports = relationship("PostReportRecord", back_populates="post", cascade="all, delete-orphan")


class PostLikeRecord(PostsBase):
    __tablename__ = "post_likes"

    like_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id = Column(
        UUID(as_uuid=True),
        ForeignKey("posts.post_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("PostRecord", back_populates="likes")

    __table_args__ = (
        UniqueConstraint("post_id", "user_id", name="uq_like_user_post"),
    )


class PostCommentRecord(PostsBase):
    __tablename__ = "post_comments"

    comment_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id = Column(
        UUID(as_uuid=True),
        ForeignKey("posts.post_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    parent_comment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("post_comments.comment_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    content = Column(Text, nullable=False)
    is_edited = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    updated_timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    post = relationship("PostRecord", back_populates="comments")
    replies = relationship(
        "PostCommentRecord",
        backref="parent",
        remote_side=[comment_id],
    )


class CategoryRecord(PostsBase):
    __tablename__ = "categories"

    category_id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(60), unique=True, index=True, nullable=False)
    label = Column(String(80), nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    post_links = relationship("PostCategoryLink", back_populates="category")


class PostCategoryLink(PostsBase):
    __tablename__ = "post_categories"

    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.category_id", ondelete="CASCADE"), primary_key=True)

    post = relationship("PostRecord", back_populates="category_links")
    category = relationship("CategoryRecord", back_populates="post_links")


class KeywordRecord(PostsBase):
    __tablename__ = "keywords"

    keyword_id = Column(Integer, primary_key=True, autoincrement=True)
    word = Column(String(80), unique=True, index=True, nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)

    post_links = relationship("PostKeywordLink", back_populates="keyword")


class PostKeywordLink(PostsBase):
    __tablename__ = "post_keywords"

    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), primary_key=True)
    keyword_id = Column(Integer, ForeignKey("keywords.keyword_id", ondelete="CASCADE"), primary_key=True)

    post = relationship("PostRecord", back_populates="keyword_links")
    keyword = relationship("KeywordRecord", back_populates="post_links")


class PendingCategoryRecord(PostsBase):
    __tablename__ = "pending_categories"

    pending_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label = Column(String(80), nullable=False)
    normalized_slug = Column(String(60), nullable=False, index=True)
    suggested_by_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(12), default="pending", nullable=False, index=True)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_timestamp = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("normalized_slug", "status", name="uq_pending_cat_slug_status"),
    )


class PostReportRecord(PostsBase):
    __tablename__ = "post_reports"

    report_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.post_id", ondelete="CASCADE"), nullable=False, index=True)
    reporter_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    
    reason = Column(Text, nullable=False)
    status = Column(String(20), default="open", nullable=False)
    created_timestamp = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("PostRecord", back_populates="reports")