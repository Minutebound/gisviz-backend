from pydantic import BaseModel, UUID4, Field, HttpUrl
from typing import List, Dict, Any, Optional
from datetime import datetime

class GeographicPublicationPayload(BaseModel):
    title: str = Field(..., max_length=255)
    description: Optional[str] = None
    primary_airport_geocode: Optional[str] = Field(None, min_length=3, max_length=3)
    
    # Governance & Forking
    parent_publication_id: Optional[UUID4] = None
    data_license: Optional[str] = "Standard Network License"
    temporal_start: Optional[datetime] = None
    temporal_end: Optional[datetime] = None
    
    tags: List[str] = []
    layer_metadata: Dict[str, Any] = {}
    geometry_geojson: Dict[str, Any]
    
    # Optional explicitly calculated bounds from the frontend (MapLibre/Turf.js)
    bounding_box_geojson: Optional[Dict[str, Any]] = None

class GeographicPublicationResponse(BaseModel):
    id: UUID4
    author_user_id: UUID4
    author_handle: str
    author_avatar_url: Optional[str] = None
    parent_publication_id: Optional[UUID4] = None
    
    title: str
    description: Optional[str] = None
    primary_airport_geocode: Optional[str] = None
    
    tags: List[str]
    layer_metadata: Dict[str, Any]
    
    # Core Spatial Payload
    geometry: Dict[str, Any]
    bounding_box: Optional[Dict[str, Any]] = None
    
    # Metadata & Metrics
    temporal_start: Optional[datetime] = None
    temporal_end: Optional[datetime] = None
    data_license: str
    
    view_count: int
    likes_count: int
    comments_count: int
    saves_count: int
    
    is_public: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CommentResponse(BaseModel):
    id: UUID4
    publication_id: UUID4
    author_user_id: UUID4
    author_handle: str     # Appended by FastAPI service logic
    author_avatar_url: Optional[str] = None 
    content: str
    created_at: datetime

    class Config:
        from_attributes = True
