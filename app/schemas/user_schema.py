from pydantic import BaseModel, EmailStr, UUID4, HttpUrl
from datetime import datetime
from typing import Optional, Dict, Any

# ----- REGISTRATION & AUTH -----
class UserRegistrationPayload(BaseModel):
    user_handle: str
    email_address: EmailStr
    plaintext_password: str

class UserAuthenticationResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_handle: str
    email_address: str
    role_name: str

class VerifyEmailPayload(BaseModel):
    email_address: EmailStr
    otp: str

class ForgotPasswordPayload(BaseModel):
    email_address: EmailStr

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
class ResetPasswordPayload(BaseModel):
    token: str
    new_password: str

# ----- PROFILE DATA -----
class RoleData(BaseModel):
    role_id: int
    name: str
    permissions: Dict[str, Any] = {}

    class Config:
        from_attributes = True

class LocationData(BaseModel):
    place: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    formatted_string: Optional[str] = None

    class Config:
        from_attributes = True

class UserProfileData(BaseModel):
    user_id: UUID4
    user_handle: str
    email_address: EmailStr
    is_verified: bool
    avatar_path: Optional[str] = None
    title: Optional[str] = None
    
    linkedin_url: Optional[str] = None
    medium_url: Optional[str] = None
    website_url: Optional[str] = None
    
    location: Optional[LocationData] = None
    role: Optional[RoleData] = None
    
    follower_count: int = 0
    following_count: int = 0
    post_count: int = 0
    created_timestamp: datetime

    class Config:
        from_attributes = True

class UserSettingsUpdatePayload(BaseModel):
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    medium_url: Optional[str] = None
    website_url: Optional[str] = None
    
    place: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None

class FollowEventData(BaseModel):
    actor_user_id: UUID4
    target_user_id: UUID4
    action: str
    occurred_timestamp: datetime

    class Config:
        from_attributes = True