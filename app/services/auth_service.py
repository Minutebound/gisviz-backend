import random
import string
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from fastapi import HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.db.models import PlatformUserRecord, RoleRecord
from app.schemas.user_schema import UserRegistrationPayload
from app.db.database import get_users_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login",
    auto_error=False,
)

DEFAULT_ROLE_NAME = "viewer"


class AuthenticationService:

    # ----------------------------------------------------------------
    # Password helpers
    # ----------------------------------------------------------------
    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        return pwd_context.hash(password)

    # ----------------------------------------------------------------
    # JWT & Roles
    # ----------------------------------------------------------------
    def create_access_token(self, subject: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )
        return jwt.encode(
            {"exp": expire, "sub": str(subject)},
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

    def _resolve_default_role(self, db: Session) -> RoleRecord | None:
        return db.query(RoleRecord).filter(RoleRecord.name == DEFAULT_ROLE_NAME).first()

    # ----------------------------------------------------------------
    # Token / OTP generators
    # ----------------------------------------------------------------
    def _generate_otp(self) -> str:
        return "".join(random.choices(string.digits, k=6))

    def _generate_secure_token(self) -> str:
        return secrets.token_urlsafe(32)

    # ----------------------------------------------------------------
    # Environment & UI helpers
    # ----------------------------------------------------------------
    @property
    def _is_prod(self) -> bool:
        """True only when the prod Docker image is running."""
        return settings.APP_ENV == "production"

    def _expose_dev_field(self, value: str) -> Optional[str]:
        """Returns value in dev/staging, None in production."""
        return None if self._is_prod else value

    # ----------------------------------------------------------------
    # Email Engine (HTML + Plaintext)
    # ----------------------------------------------------------------
    def _build_html_email(self, title: str, content_html: str) -> str:
        """Wraps email content in a branded HTML template."""
        logo_url = "https://ias.uicdn.net/fileadmin/IONOS/user_upload/shield_user_red.svg?h=75d2ab219a0a2cf21e516a3ac3b905c2c4088523"
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f9fafb; padding: 40px 20px; margin: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
                .header {{ background-color: #111827; padding: 20px; text-align: center; color: #ffffff; font-weight: bold; letter-spacing: 1px; font-family: monospace; }}
                .content {{ padding: 30px; color: #374151; line-height: 1.6; font-size: 16px; }}
                .footer {{ background-color: #f3f4f6; padding: 30px; text-align: center; border-top: 1px solid #e5e7eb; }}
                .logo {{ max-width: 40px; height: auto; margin-bottom: 10px; opacity: 0.8; }}
                .footer-text {{ font-size: 12px; color: #6b7280; font-family: monospace; margin: 0; }}
                .otp-box {{ display: inline-block; background: #f3f4f6; border: 1px solid #e5e7eb; padding: 15px 30px; font-size: 24px; font-weight: bold; letter-spacing: 5px; font-family: monospace; color: #F54438; border-radius: 6px; margin: 20px 0; }}
                .btn {{ display: inline-block; background-color: #f3f4f6; color: #F54438; text-decoration: none; padding: 12px 24px; border-radius: 6px; font-weight: bold; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">{title}</div>
                <div class="content">
                    {content_html}
                </div>
                <div class="footer">
                   <img src="{logo_url}" alt="Gisviz Logo" class="logo" style="filter: grayscale(100%);" />
                    <p class="footer-text">© {datetime.now(timezone.utc).year} Gisviz. All rights reserved.</p>
                    <p class="footer-text">This is an automated system message. Please do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """

    def _email(self, to_email: str, subject: str, plain_body: str, html_body: str) -> None:
        """Routes to console (dev) or SMTP (prod)."""
        if self._is_prod:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"Gisviz Security <{settings.SMTP_USER}>"
            msg["To"] = to_email
            
            msg.attach(MIMEText(plain_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            try:
                with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                    server.login(settings.SMTP_USER, settings.SMTP_PASS)
                    server.sendmail(settings.SMTP_USER, to_email, msg.as_string())
            except Exception as e:
                print(f"SMTP Error: {e}")
                raise HTTPException(status_code=500, detail="Failed to send email")
        else:
            print(f"\n[{datetime.now(timezone.utc)}] EMAIL TO: {to_email}")
            print(f"SUBJECT: {subject}")
            print(f"CONTENT:\n{plain_body}\n")

    # ----------------------------------------------------------------
    # Auth flows
    # ----------------------------------------------------------------
    def register_new_user(self, db: Session, payload: UserRegistrationPayload):
        if db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == payload.email_address).first():
            raise HTTPException(status_code=400, detail="Email already registered")
        if db.query(PlatformUserRecord).filter(PlatformUserRecord.user_handle == payload.user_handle).first():
            raise HTTPException(status_code=400, detail="Handle already taken")

        default_role = self._resolve_default_role(db)
        otp = self._generate_otp()
        new_user = PlatformUserRecord(
            user_handle=payload.user_handle,
            email_address=payload.email_address,
            hashed_security_password=self.get_password_hash(payload.plaintext_password),
            role_id=default_role.role_id if default_role else None,
            is_verified=0,
            is_active=1,
            verification_otp=otp,
            otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        html_content = self._build_html_email(
            title="Account Verification",
            content_html=f"""
                <p>Hello <strong>@{payload.user_handle}</strong>,</p>
                <p>Thank you for registering. Please use the verification code below to activate your account:</p>
                <div style="text-align: center;"><div class="otp-box">{otp}</div></div>
                <p><em>This code will expire in 15 minutes.</em></p>
            """
        )

        self._email(
            to_email=payload.email_address,
            subject="Verify your Gisviz Account",
            plain_body=f"Your verification code is: {otp}. It expires in 15 minutes.",
            html_body=html_content
        )

        return {"user": new_user, "dev_otp": self._expose_dev_field(otp)}

    def resend_verification_otp(self, db: Session, email_address: str):
        """Generates and sends a fresh OTP for an unverified user."""
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == email_address).first()
        
        # Don't reveal if email exists to prevent enumeration
        if not user or user.is_verified == 1:
            return {"status": "If the account exists and is unverified, a new code has been sent."}

        otp = self._generate_otp()
        user.verification_otp = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()

        html_content = self._build_html_email(
            title="New Verification Code",
            content_html=f"""
                <p>Hello <strong>@{user.user_handle}</strong>,</p>
                <p>You requested a new verification code. Please use the code below to activate your account:</p>
                <div style="text-align: center;"><div class="otp-box">{otp}</div></div>
                <p><em>This code will expire in 15 minutes.</em></p>
            """
        )

        self._email(
            to_email=email_address,
            subject="Your new Gisviz Verification Code",
            plain_body=f"Your new verification code is: {otp}. It expires in 15 minutes.",
            html_body=html_content
        )

        return {
            "status": "If the account exists and is unverified, a new code has been sent.",
            "dev_otp": self._expose_dev_field(otp)
        }

    def verify_email(self, db: Session, email_address: str, otp: str):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == email_address).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.is_verified == 1 and user.is_active == 1:
            return {"status": "Already verified"}
        if user.verification_otp != otp:
            raise HTTPException(status_code=400, detail="Invalid OTP")
        if user.otp_expires_at and datetime.now(timezone.utc) > user.otp_expires_at:
            raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

        user.is_verified = 1
        user.is_active = 1
        user.verification_otp = None
        user.otp_expires_at = None
        db.commit()

        return {"status": "Email verified successfully", "reactivated": True}

    def initiate_password_reset(self, db: Session, email_address: str):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == email_address).first()
        if not user:
            return {"status": "If the email exists, a reset link has been sent."}

        token = self._generate_secure_token()
        user.password_reset_token = token
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.commit()

        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        
        html_content = self._build_html_email(
            title="Password Reset Request",
            content_html=f"""
                <p>Hello <strong>@{user.user_handle}</strong>,</p>
                <p>We received a request to reset your password. Click below to set a new password:</p>
                <div style="text-align: center;"><a href="{reset_link}" class="btn">Reset Password</a></div>
                <p>Or paste this link into your browser: <br/><span style="font-size:12px;color:#6b7280;">{reset_link}</span></p>
            """
        )

        self._email(
            to_email=email_address,
            subject="Gisviz Password Reset Request",
            plain_body=f"Reset your password here: {reset_link}",
            html_body=html_content
        )

        return {
            "status": "If the email exists, a reset link has been sent.",
            "dev_token": self._expose_dev_field(token),
        }

    def execute_password_reset(self, db: Session, token: str, new_password: str):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.password_reset_token == token).first()
        if not user:
            raise HTTPException(status_code=400, detail="Invalid reset token")
        if user.reset_token_expires_at and datetime.now(timezone.utc) > user.reset_token_expires_at:
            raise HTTPException(status_code=400, detail="Reset token has expired")

        user.hashed_security_password = self.get_password_hash(new_password)
        user.password_reset_token = None
        user.reset_token_expires_at = None
        db.commit()
        return {"status": "Password has been successfully reset"}


# ----------------------------------------------------------------
# Singleton used by all endpoint routers
# ----------------------------------------------------------------
auth_service = AuthenticationService()


# ----------------------------------------------------------------
# FastAPI dependencies (THESE WERE MISSING!)
# ----------------------------------------------------------------

def get_current_authenticated_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_users_db),
) -> PlatformUserRecord:
    """Requires a valid JWT — raises 401 if missing or invalid."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id == user_id
    ).first()
    if user is None:
        raise credentials_exception
    if user.is_verified == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account email not verified.",
        )
    if user.is_active == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account has been deactivated.",
        )
    return user


def get_optional_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_users_db),
) -> Optional[PlatformUserRecord]:
    """
    Same as get_current_authenticated_user but returns None instead of
    raising 401 when no token is present or the token is invalid.
    Used by public endpoints (stream, post detail) that annotate
    is_liked / is_bookmarked only when a user is authenticated.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None

    user = db.query(PlatformUserRecord).filter(
        PlatformUserRecord.user_id == user_id
    ).first()
    if user is None or user.is_verified == 0 or user.is_active == 0:
        return None
    return user


class RoleChecker:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(
        self,
        user: PlatformUserRecord = Depends(get_current_authenticated_user),
    ):
        role_name = user.role.name if user.role else "viewer"
        if role_name == "admin":
            return user
        if role_name not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation not permitted. Requires one of: {self.allowed_roles}",
            )
        return user