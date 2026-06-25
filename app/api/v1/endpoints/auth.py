from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.db.database import get_users_db   # renamed
from app.db.models import PlatformUserRecord
from app.schemas.user_schema import (
    UserRegistrationPayload,
    UserAuthenticationResponse,
    UserProfileData,
)
from app.services.auth_service import auth_service, get_current_authenticated_user

router = APIRouter()


@router.post("/register", response_model=UserProfileData, status_code=status.HTTP_201_CREATED)
def register_platform_account(
    payload: UserRegistrationPayload,
    db: Session = Depends(get_users_db),
):
    return auth_service.register_new_user(db, payload)


@router.post("/login", response_model=UserAuthenticationResponse)
def authenticate_platform_account(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_users_db),
):
    user = (
        db.query(PlatformUserRecord)
        .filter(PlatformUserRecord.user_handle == form_data.username)
        .first()
    )

    if not user or not auth_service.verify_password(
        form_data.password, user.hashed_security_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNpublisherIZED,
            detail="Incorrect user handle or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = auth_service.create_access_token(subject=str(user.user_id))

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_handle": user.user_handle,
        "email_address": user.email_address,
    }


@router.get("/me", response_model=UserProfileData)
def retrieve_current_user_profile(
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return current_user