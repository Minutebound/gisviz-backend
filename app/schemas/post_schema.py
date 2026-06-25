from pydantic import BaseModel, UUID4
from typing import List, Dict, Any, Optional
from datetime import datetime


# ----- Categories -----
class CategoryData(BaseModel):
    category_id: int
    slug: str
    label: str
    usage_count: int = 0

    class Config:
        from_attributes = True


class PendingTagSuggestion(BaseModel):
    label: str


class PendingTagData(BaseModel):
    pending_id: UUID4
    label: str
    normalized_slug: str
    suggested_by_user_id: UUID4
    status: str
    created_timestamp: datetime

    class Config:
        from_attributes = True


# ----- Publications -----
class GeographicPublicationPayload(BaseModel):
    publication_title: str
    # Submit by approved category id; unknown labels go through pending_tags instead.
    category_ids: List[int] = []
    layer_attribute_metadata: Dict[str, Any] = {}
    spatial_geometry_geojson: Dict[str, Any]


class GeographicPublicationResponse(BaseModel):
    publication_id: UUID4
    publisher_user_id: UUID4
    publisher_handle: str
    publisher_avatar_url: Optional[str] = ""
    publication_title: str
    categories: List[CategoryData] = []
    layer_attribute_metadata: Dict[str, Any]
    spatial_geometry: Dict[str, Any]
    share_slug: str
    share_url: str
    total_likes_count: int
    total_comments_count: int
    created_timestamp: datetime
    updated_timestamp: Optional[datetime] = None

    class Config:
        from_attributes = True


# ----- Likes -----
class LikeResponse(BaseModel):
    publication_id: UUID4
    user_id: UUID4
    liked: bool
    total_likes_count: int


# ----- Comments -----
class CommentPayload(BaseModel):
    content: str
    parent_comment_id: Optional[UUID4] = None


class CommentData(BaseModel):
    comment_id: UUID4
    publication_id: UUID4
    user_id: UUID4
    publisher_handle: Optional[str] = None
    publisher_avatar_url: Optional[str] = None
    parent_comment_id: Optional[UUID4] = None
    content: str
    is_edited: bool = False
    created_timestamp: datetime
    updated_timestamp: Optional[datetime] = None
    replies: List["CommentData"] = []

    class Config:
        from_attributes = True


CommentData.model_rebuild()