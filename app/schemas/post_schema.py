from pydantic import BaseModel, UUID4
from typing import List, Optional, Union
from datetime import datetime

class CategoryData(BaseModel):
    category_id: int
    slug: str = ""
    label: str
    usage_count: int = 0
    description: Optional[str] = None
    theme_color: Optional[str] = None      # #rrggbb; the accent of posts in this category
    sort_order: int = 0
    banner: Optional[dict] = None          # the feed banner's animated scene (banner_service)

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


class VisualParams(BaseModel):
    """The viewer's visual choice for a dataset-backed post (validated again on the server)."""
    viz: Optional[str] = None
    x: Optional[str] = None
    y: Optional[str] = None
    label_field: Optional[str] = None
    z: Optional[str] = None        # series / second category (stacked bar, heatmap)
    size: Optional[str] = None     # numeric column for bubble size
    color: Optional[str] = None    # category column that colours the bars, with a legend (e.g. continent)
    visual_type: Optional[str] = None   # catalog code (misc DB visual_types), e.g. choropleth; derived when empty
    # presentation ("story") options, validated by visual_service._story
    title: Optional[str] = None
    subtitle: Optional[str] = None
    y_label: Optional[str] = None                      # friendly name of the measure
    colors: Optional[dict] = None                      # {group: "#rrggbb"}
    # measures drawn over the chart: [{"value"|"stat": mean|median|mode, "label": ..., "mode": "line"|"divider",
    #                                   "axis": "x"|"y", "shade": "below"|"above"}]
    reference_lines: Optional[List[dict]] = None
    filter_default: Optional[List[str]] = None         # labels pre-selected in the post page's filter (None = all)
    show_labels: Optional[bool] = None                 # labels drawn on the map / points from the start (reader can toggle)
    x_scale: Optional[str] = None                      # scatter / bubble axes: "linear" (default) or "log"
    y_scale: Optional[str] = None
    show_summary: Optional[bool] = None                # scatter / bubble: a mean / median / mode strip under the chart

    def story(self) -> dict:
        return {k: getattr(self, k) for k in ("title", "subtitle", "y_label", "colors", "reference_lines",
                                              "filter_default", "show_labels", "visual_type",
                                              "x_scale", "y_scale", "show_summary") if getattr(self, k) is not None}


class PostPayload(BaseModel):
    title: str
    description: Optional[str] = None
    # Posts are built from a dataset: dataset_id (+ visual params) renders from DuckDB.
    # Required when creating; on edit, omit it to keep the current visual.
    dataset_id: Optional[str] = None
    visual_params: Optional[VisualParams] = None
    theme_color: Optional[str] = None      # hex like #06ba24, saved on the post and used to colour the visual
    note: Optional[str] = None
    # ignored: a post's source is its dataset's source (kept so older clients still validate)
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    category_ids: List[int] = []          # at least one (posts DB categories)
    region: Optional[str] = None          # required: regions.code (misc DB), e.g. "europe" or "global"
    keywords: List[str] = []
    # the poster behind the visual: a design from the poster editor {bg, texture, font, title_size, title_align,
    # map_base, layout}, or "auto" (picked from the topic) / "light" / "dark" / "none". Omitted on edit = keep it.
    backdrop: Optional[Union[dict, str]] = "auto"
    # who sees it: public (feed, search, profile) | private (only you, admins/editors, and who you share it with)
    # omitted: public for a new post, unchanged on edit
    visibility: Optional[str] = None
    # standard | live — informational: a post on a STREAM dataset is always live (refreshes itself)
    post_type: Optional[str] = None


class BackdropPreviewPayload(BaseModel):
    title: str = ""
    description: Optional[str] = None
    category_ids: List[int] = []
    keywords: List[str] = []
    region: Optional[str] = None
    theme_color: Optional[str] = None
    backdrop: Optional[Union[dict, str]] = "auto"


class PostResponse(BaseModel):
    post_id: UUID4
    publisher_user_id: UUID4
    publisher_handle: str
    publisher_avatar_path: Optional[str] = None
    title: str
    description: Optional[str] = None
    visual_image_path: Optional[str] = None
    dataset_id: Optional[str] = None
    visual_spec: Optional[dict] = None
    theme_color: Optional[str] = None
    dataset: Optional[dict] = None          # linked dataset metadata (title, source, licence, rows...)
    note: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    categories: List[CategoryData] = []
    region: Optional[str] = None
    keywords: List[KeywordData] = []
    share_slug: str
    share_url: str
    total_likes_count: int
    total_comments_count: int
    views_count: int = 0
    backdrop: Optional[dict] = None         # the poster design: {mode, bg, texture, font, title_size, title_align, map_base, layout, dark}
    created_timestamp: datetime
    updated_timestamp: Optional[datetime] = None
    is_active: int
    is_liked: Optional[bool] = None
    is_bookmarked: Optional[bool] = None
    visibility: str = "public"              # public | private
    post_type: str = "standard"             # standard | live
    live: Optional[dict] = None             # live posts: {refresh_seconds, updated_at}
    can_manage: Optional[bool] = None       # the viewer may edit / share it
    shared_by: Optional[dict] = None        # "Shared" tab: {handle, label, at, via}

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