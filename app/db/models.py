import uuid
from sqlalchemy import Column, String, DateTime, Integer, Boolean, ForeignKey, Text, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from geoalchemy2 import Geometry

from app.db.database import AuthBase, SpatialBase

# ==========================================
# 1. AUTHENTICATION & IDENTITY (AuthBase)
# ==========================================

class User(AuthBase):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    handle = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=True) # Nullable for SSO users
    
    # SSO & Identity
    auth_provider = Column(String(50), default="local") # local, google, github
    auth_provider_id = Column(String(255), nullable=True, index=True)
    
    # RBAC & SaaS
    role = Column(String(20), default="user") # user, creator, moderator, admin
    subscription_tier = Column(String(50), default="free") 
    
    # Profile & State
    avatar_url = Column(String, nullable=True)
    bio = Column(String(500), nullable=True)
    preferences = Column(JSONB, default={"theme": "system", "default_projection": "EPSG:4326"})
    
    # Analytics & Engagement (NEW)
    follower_count = Column(Integer, default=0)
    following_count = Column(Integer, default=0)
    total_posts_count = Column(Integer, default=0)
    total_received_likes = Column(Integer, default=0)
    total_received_saves = Column(Integer, default=0)
    last_active_at = Column(DateTime(timezone=True), server_default=func.now())
    
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class UserFollow(AuthBase):
    __tablename__ = "user_follows"
    follower_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    following_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ==========================================
# 2. SPATIAL & GEOGRAPHIC (SpatialBase)
# ==========================================

class Publication(SpatialBase):
    __tablename__ = "publications"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author_user_id = Column(UUID(as_uuid=True), nullable=False, index=True) 
    
    # Social Forks
    parent_publication_id = Column(UUID(as_uuid=True), ForeignKey("publications.id", ondelete="SET NULL"), nullable=True)
    
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    primary_airport_geocode = Column(String(3), index=True, nullable=True)
    
    # Performance Geometry (Caching the Bounding Box)
    geometry = Column(Geometry(geometry_type='GEOMETRY', srid=4326, spatial_index=True), nullable=False)
    bounding_box = Column(Geometry(geometry_type='POLYGON', srid=4326), nullable=True)
    
    # Temporal Data & Governance
    temporal_start = Column(DateTime(timezone=True), nullable=True)
    temporal_end = Column(DateTime(timezone=True), nullable=True)
    data_license = Column(String(100), default="Standard Network License")
    
    layer_metadata = Column(JSONB, default=dict)
    tags = Column(ARRAY(String), default=list)
    
    # Deep Analytics (UPDATED)
    view_count = Column(Integer, default=0)
    likes_count = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    saves_count = Column(Integer, default=0) 
    shares_count = Column(Integer, default=0)       # NEW: Virality
    engagement_rate = Column(Integer, default=0)    # NEW: (Likes+Comments+Shares+Saves)/Views * 100
    
    is_public = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    comments = relationship("Comment", back_populates="publication", cascade="all, delete-orphan")
    likes = relationship("Like", back_populates="publication", cascade="all, delete-orphan")
    saves = relationship("Bookmark", back_populates="publication", cascade="all, delete-orphan")

class Bookmark(SpatialBase):
    __tablename__ = "bookmarks"
    publication_id = Column(UUID(as_uuid=True), ForeignKey("publications.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    publication = relationship("Publication", back_populates="saves")

class Comment(SpatialBase):
    __tablename__ = "comments"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id = Column(UUID(as_uuid=True), ForeignKey("publications.id", ondelete="CASCADE"), nullable=False, index=True)
    author_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    publication = relationship("Publication", back_populates="comments")

class Like(SpatialBase):
    __tablename__ = "likes"
    
    publication_id = Column(UUID(as_uuid=True), ForeignKey("publications.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), primary_key=True, index=True) # ID from AuthDB
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    publication = relationship("Publication", back_populates="likes")

# ==========================================
# 3. BACKWARD COMPATIBILITY ALIASES
# ==========================================
PlatformUserRecord = User
GeographicPublicationRecord = Publication