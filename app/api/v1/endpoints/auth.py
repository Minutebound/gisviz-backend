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
    ChangePasswordRequest,
)
from app.services.auth_service import auth_service, get_current_authenticated_user
from app.db.models import PlatformUserRecord

router = APIRouter()


@router.post("/register")
def register(payload: UserRegistrationPayload, db: Session = Depends(get_users_db)):
    result = auth_service.register_new_user(db=db, payload=payload)

    # dev_otp is None in production (auth_service._expose_dev_field returns None).
    # Only add the key to the response when it has a value.
    response = {
        "message": "Registration successful. Please check your email for the verification code.",
        "user_handle": result["user"].user_handle,
    }
    if result["dev_otp"] is not None:
        response["dev_otp"] = result["dev_otp"]
    return response


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
    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.email_address == form_data.username
    ).first()
    if not user or not auth_service.verify_password(
        form_data.password, user.hashed_security_password
    ):
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    # ── Unverified account ───────────────────────────────────────────
    if user.is_verified == 0:
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        # dev  → prints OTP to console
        # prod → sends real email to the user's inbox
        auth_service._email(
            to_email=user.email_address,
            subject="Account Verification Code",
            body=(
                f"A login attempt was made on your account.\n"
                f"Your verification code is: {otp}\n"
                "It expires in 15 minutes."
            ),
        )

        # Build the 403 detail — only include dev_otp outside production
        detail: dict = {"error": "unverified", "email": user.email_address}
        dev_otp = auth_service._expose_dev_field(otp)
        if dev_otp is not None:
            detail["dev_otp"] = dev_otp

        raise HTTPException(status_code=403, detail=detail)

    # ── Deactivated account ──────────────────────────────────────────
    if user.is_active == 0:
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        auth_service._email(
            to_email=user.email_address,
            subject="Account Reactivation — Verify your identity",
            body=(
                f"A login attempt was made on your deactivated account.\n"
                f"If this was you and you want to reactivate it, enter this code: {otp}\n"
                "It expires in 15 minutes."
            ),
        )

        detail = {"error": "deactivated", "email": user.email_address}
        dev_otp = auth_service._expose_dev_field(otp)
        if dev_otp is not None:
            detail["dev_otp"] = dev_otp

        raise HTTPException(status_code=403, detail=detail)

    # ── Happy path ───────────────────────────────────────────────────
    access_token = auth_service.create_access_token(subject=str(user.user_id))
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_handle": user.user_handle,
        "email_address": user.email_address,
        "role_name": user.role.name if user.role else "viewer",
    }


@router.post("/resend-otp")
def resend_otp(payload: ForgotPasswordPayload, db: Session = Depends(get_users_db)):
    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.email_address == payload.email_address
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    otp = auth_service._generate_otp()
    user.verification_otp = otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.commit()

    auth_service._email(
        to_email=user.email_address,
        subject="New Verification Code",
        body=f"Your new verification code is: {otp}. It expires in 15 minutes.",
    )

    response = {"message": "New OTP sent successfully."}
    dev_otp = auth_service._expose_dev_field(otp)
    if dev_otp is not None:
        response["dev_otp"] = dev_otp
    return response


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_users_db)):
    # auth_service.initiate_password_reset already calls _email() and
    # guards dev_token via _expose_dev_field — nothing extra needed here.
    return auth_service.initiate_password_reset(
        db=db, email_address=payload.email_address
    )


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

    current_user.hashed_security_password = auth_service.get_password_hash(
        payload.new_password
    )
    db.commit()
    return {"status": "success", "message": "Password updated successfully."}