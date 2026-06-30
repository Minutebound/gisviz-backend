from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

from app.db.database import get_users_db
from app.schemas.user_schema import (
    UserRegistrationPayload, 
    UserAuthenticationResponse, 
    VerifyEmailPayload,
    ForgotPasswordPayload,
    ResetPasswordPayload,
    ChangePasswordRequest
)
from app.services.auth_service import auth_service, get_current_authenticated_user
from app.db.models import PlatformUserRecord

router = APIRouter()

@router.post("/register")
def register(payload: UserRegistrationPayload, db: Session = Depends(get_users_db)):
    result = auth_service.register_new_user(db=db, payload=payload)
    return {
        "message": "Registration successful. Please check your email for the verification code.",
        "user_handle": result["user"].user_handle,
        "dev_otp": result["dev_otp"] 
    }

@router.post("/verify")
def verify_email(payload: VerifyEmailPayload, db: Session = Depends(get_users_db)):
    return auth_service.verify_email(db=db, email_address=payload.email_address, otp=payload.otp)

@router.post("/login", response_model=UserAuthenticationResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_users_db)):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == form_data.username).first()
    if not user or not auth_service.verify_password(form_data.password, user.hashed_security_password):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == form_data.username).first()
        if not user or not auth_service.verify_password(form_data.password, user.hashed_security_password):
            raise HTTPException(status_code=400, detail="Incorrect username/email or password")

    if user.is_verified == 0:
        raise HTTPException(
            status_code=403, 
            detail={"error": "unverified", "email": user.email_address}
        )

    access_token = auth_service.create_access_token(subject=str(user.user_id))
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_handle": user.user_handle,
        "email_address": user.email_address,
        "role_name": user.role.name if user.role else "viewer"
    }

@router.post("/resend-otp")
def resend_otp(payload: ForgotPasswordPayload, db: Session = Depends(get_users_db)):
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == payload.email_address).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    otp = auth_service._generate_otp()
    user.verification_otp = otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.commit()
    
    auth_service.send_email(user.email_address, "New Verification Code", f"Your new code is {otp}")
    return {"message": "New OTP sent successfully."}

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_users_db)):
    return auth_service.initiate_password_reset(db=db, email_address=payload.email_address)

@router.post("/reset-password")
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_users_db)):
    return auth_service.execute_password_reset(db=db, token=payload.token, new_password=payload.new_password)

@router.put("/change-password")
def change_password(
    payload: ChangePasswordRequest, 
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user)
):
    """Allows an authenticated user to change their password securely."""
    if not auth_service.verify_password(payload.current_password, current_user.hashed_security_password):
        raise HTTPException(status_code=400, detail="Incorrect current password. Please try again.")
    
    current_user.hashed_security_password = auth_service.get_password_hash(payload.new_password)
    db.commit()
    
    return {"status": "success", "message": "Password updated successfully."}