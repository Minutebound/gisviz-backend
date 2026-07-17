from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, EmailStr

from app.db.database import get_users_db
from app.schemas.user_schema import (
    UserRegistrationPayload,
    UserAuthenticationResponse,
    VerifyEmailPayload,
    ForgotPasswordPayload,
    ResetPasswordPayload,
    ChangePasswordRequest,
)
from app.services.auth_service import auth_service, get_current_authenticated_user
from app.db.models import PlatformUserRecord

router = APIRouter()

# Schema for the resend OTP route
class ResendOtpPayload(BaseModel):
    email_address: EmailStr


@router.post("/register")
def register(payload: UserRegistrationPayload, db: Session = Depends(get_users_db)):
    result = auth_service.register_new_user(db=db, payload=payload)
    return {
        "message": "Registration successful. Please check your email for the verification code.",
        "user_handle": result["user"].user_handle,
        "dev_otp": result.get("dev_otp"),
    }


@router.post("/verify")
def verify_email(payload: VerifyEmailPayload, db: Session = Depends(get_users_db)):
    return auth_service.verify_email(
        db=db, email_address=payload.email_address, otp=payload.otp
    )


@router.post("/login", response_model=UserAuthenticationResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_users_db),
):
    # Try handle first, fall back to email
    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.email_address == form_data.username
    ).first()
    
    if not user or not auth_service.verify_password(
        form_data.password, user.hashed_security_password
    ):
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    # Unverified — redirect to OTP view
    if user.is_verified == 0:
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        # Build HTML Email
        html_content = auth_service._build_html_email(
            title="Account Verification Code",
            content_html=f"""
                <p>Hello <strong>@{user.user_handle}</strong>,</p>
                <p>A login attempt was made for your unverified account. Please use the verification code below to activate your account:</p>
                <div style="text-align: center;"><div class="otp-box">{otp}</div></div>
                <p><em>This code will expire in 15 minutes.</em></p>
            """
        )

        auth_service._email(
            to_email=user.email_address,
            subject="Account Verification Code",
            plain_body=f"A login attempt was made. Your verification code is {otp}. It expires in 15 minutes.",
            html_body=html_content
        )

        raise HTTPException(
            status_code=403,
            detail={
                "error": "unverified", 
                "email": user.email_address,
                "dev_otp": auth_service._expose_dev_field(otp) 
            },
        )

    # Deactivated account — reactivate flow
    if user.is_active == 0:
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        html_content = auth_service._build_html_email(
            title="Account Reactivation",
            content_html=f"""
                <p>Hello <strong>@{user.user_handle}</strong>,</p>
                <p>A login attempt was made on your deactivated account.</p>
                <p>If this was you and you want to reactivate your account, enter the code below:</p>
                <div style="text-align: center;"><div class="otp-box">{otp}</div></div>
                <p><em>This code will expire in 15 minutes.</em></p>
            """
        )

        auth_service._email(
            to_email=user.email_address,
            subject="Account Reactivation — Verify your identity",
            plain_body=f"A login attempt was made on your deactivated account. Your reactivation code is: {otp}. It expires in 15 minutes.",
            html_body=html_content
        )

        raise HTTPException(
            status_code=403,
            detail={
                "error": "deactivated",
                "email": user.email_address,
                "dev_otp": auth_service._expose_dev_field(otp),
            },
        )

    access_token = auth_service.create_access_token(subject=str(user.user_id))
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_handle": user.user_handle,
        "email_address": user.email_address,
        "role_name": user.role.name if user.role else "viewer",
    }


@router.post("/resend-otp")
def resend_otp(payload: ResendOtpPayload, db: Session = Depends(get_users_db)):
    # This automatically delegates to the service which handles HTML generation and sending
    return auth_service.resend_verification_otp(db=db, email_address=payload.email_address)


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_users_db)):
    return auth_service.initiate_password_reset(db=db, email_address=payload.email_address)


@router.post("/reset-password")
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_users_db)):
    return auth_service.execute_password_reset(
        db=db, token=payload.token, new_password=payload.new_password
    )


@router.put("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    if not auth_service.verify_password(
        payload.current_password, current_user.hashed_security_password
    ):
        raise HTTPException(status_code=400, detail="Incorrect current password.")

    current_user.hashed_security_password = auth_service.get_password_hash(payload.new_password)
    db.commit()
    return {"status": "success", "message": "Password updated successfully."}