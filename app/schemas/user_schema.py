from pydantic import BaseModel, EmailStr, UUID4, Field
from typing import Optional, List
from datetime import datetime

class UserRegistrationPayload(BaseModel):
    handle: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8)

class UserAuthenticationResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID4
    handle: str
    email: str

class UserProfileData(BaseModel):
    id: UUID4
    handle: str
    email: EmailStr
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    is_verified: bool
    subscription_tier: str
    expert_level: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class UserProfileUpdate(BaseModel):
    handle: Optional[str] = Field(None, min_length=3, max_length=50)
    bio: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = None

class FollowActionResponse(BaseModel):
    status: str  # Will return "followed" or "unfollowed"
    follower_count: int

class NetworkUserItem(BaseModel):
    handle: str
    avatar_url: Optional[str]
    followed_at: datetime
    days_following: int  # Dynamic timeframe calculation

class NetworkListResponse(BaseModel):
    users: List[NetworkUserItem]
    total_count: int