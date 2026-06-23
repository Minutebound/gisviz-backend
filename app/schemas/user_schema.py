from pydantic import BaseModel, EmailStr, UUID4
from datetime import datetime
from typing import Optional, Dict, Any


class UserRegistrationPayload(BaseModel):
    user_handle: str
    email_address: EmailStr
    plaintext_password: str


class UserAuthenticationResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_handle: str
    email_address: str


class RoleData(BaseModel):
    role_id: int
    name: str
    permissions: Dict[str, Any] = {}

    class Config:
        from_attributes = True


class UserProfileData(BaseModel):
    user_id: UUID4
    user_handle: str
    email_address: EmailStr
    avatar_storage_url: Optional[str] = None
    role: Optional[RoleData] = None
    follower_count: int = 0
    following_count: int = 0
    publication_count: int = 0
    created_timestamp: datetime

    class Config:
        from_attributes = True


class FollowEventData(BaseModel):
    actor_user_id: UUID4
    target_user_id: UUID4
    action: str
    occurred_timestamp: datetime

    class Config:
        from_attributes = True