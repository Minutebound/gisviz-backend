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
    return {
        "message": "Registration successful. Please check your email for the verification code.",
        "user_handle": result["user"].user_handle,
        "dev_otp": result["dev_otp"],
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
        PlatformUserRecord.user_handle == form_data.username
    ).first()
    if not user or not auth_service.verify_password(
        form_data.password, user.hashed_security_password
    ):
        user = db.query(PlatformUserRecord).filter(
            PlatformUserRecord.email_address == form_data.username
        ).first()
        if not user or not auth_service.verify_password(
            form_data.password, user.hashed_security_password
        ):
            raise HTTPException(status_code=400, detail="Incorrect username/email or password")

    # Unverified — redirect to OTP view
    if user.is_verified == 0:
        # Generate a fresh OTP to ensure it isn't expired
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        auth_service.simulate_send_email(
            email=user.email_address,
            subject="Account Verification Code",
            content=f"A login attempt was made. Your verification code is {otp}. It expires in 15 minutes.",
        )

        raise HTTPException(
            status_code=403,
            detail={
                "error": "unverified", 
                "email": user.email_address,
                "dev_otp": otp  # Included for dev mode
            },
        )

    # Deactivated account — reactivate it, generate a fresh OTP, and
    # send them back through the verification flow. This covers the
    # "deleted account trying to log back in" case: they verify the OTP,
    # which re-activates the account and lets them in.
    if user.is_active == 0:
        otp = auth_service._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        auth_service.simulate_send_email(
            email=user.email_address,
            subject="Account Reactivation — Verify your identity",
            content=(
                f"A login attempt was made on your deactivated account.\n"
                f"If this was you and you want to reactivate your account, "
                f"enter this code: {otp}\n"
                f"It expires in 15 minutes."
            ),
        )

        raise HTTPException(
            status_code=403,
            detail={
                "error": "deactivated",
                "email": user.email_address,
                "dev_otp": otp, # Included for dev mode
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

    auth_service.simulate_send_email(
        email=user.email_address,
        subject="New Verification Code",
        content=f"Your new code is {otp}. It expires in 15 minutes.",
    )
    return {"message": "New OTP sent successfully.", "dev_otp": otp}


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