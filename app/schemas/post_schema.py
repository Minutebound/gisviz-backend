from pydantic import BaseModel, UUID4
from typing import List, Optional
from datetime import datetime


class CategoryData(BaseModel):
    category_id: int
    slug: str = ""
    label: str
    usage_count: int = 0

    class Config:
        from_attributes = True


class KeywordData(BaseModel):
    keyword_id: int
    word: str

    class Config:
        from_attributes = True


class PendingCategorySuggestion(BaseModel):
    label: str


class PendingCategoryData(BaseModel):
    pending_id: UUID4
    label: str
    normalized_slug: str
    suggested_by_user_id: UUID4
    status: str
    created_timestamp: datetime

    class Config:
        from_attributes = True


class PostPayload(BaseModel):
    title: str
    description: Optional[str] = None
    visual_image_path: str
    note: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    category_ids: List[int] = []
    keywords: List[str] = []


class PostResponse(BaseModel):
    post_id: UUID4
    publisher_user_id: UUID4
    publisher_handle: str
    publisher_avatar_path: Optional[str] = None
    title: str
    description: Optional[str] = None
    visual_image_path: Optional[str] = None
    note: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    categories: List[CategoryData] = []
    keywords: List[KeywordData] = []
    share_slug: str
    share_url: str
    total_likes_count: int
    total_comments_count: int
    created_timestamp: datetime
    updated_timestamp: Optional[datetime] = None
    # Per-user flags — None when the request is unauthenticated
    is_liked: Optional[bool] = None
    is_bookmarked: Optional[bool] = None

    class Config:
        from_attributes = True


class PostReportPayload(BaseModel):
    reason: str


class PostReportResponse(BaseModel):
    report_id: UUID4
    post_id: UUID4
    reporter_user_id: UUID4
    reason: str
    status: str
    created_timestamp: datetime

    class Config:
        from_attributes = True

class ReportStatusPayload(BaseModel):
    status: str  # "open" | "resolved" | "dismissed"
    
class LikeResponse(BaseModel):
    post_id: UUID4
    user_id: UUID4
    liked: bool
    total_likes_count: int


class BookmarkResponse(BaseModel):
    post_id: UUID4
    user_id: UUID4
    bookmarked: bool


class CommentPayload(BaseModel):
    content: str
    parent_comment_id: Optional[UUID4] = None


class CommentData(BaseModel):
    comment_id: UUID4
    post_id: UUID4
    user_id: UUID4
    publisher_handle: Optional[str] = None
    publisher_avatar_path: Optional[str] = None
    parent_comment_id: Optional[UUID4] = None
    content: str
    is_edited: bool = False
    created_timestamp: datetime
    updated_timestamp: Optional[datetime] = None
    replies: List["CommentData"] = []

    class Config:
        from_attributes = True


CommentData.model_rebuild()