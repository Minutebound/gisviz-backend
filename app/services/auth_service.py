import uuid
import random
import string
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import ExpiredSignatureError, JWTError, jwt
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
    tokenUrl=f"{settings.API_VERSION_STR}/auth/login",
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
    def create_access_token(self, subject: str, token_version: int = 0) -> str:
        """Session token. No expiry when JWT_ACCESS_TOKEN_EXPIRE_MINUTES is 0 (the default): the
        session lasts until logout (jti revoked) or a password change/reset (token_version bumped)."""
        now = datetime.now(timezone.utc)
        claims = {"sub": str(subject), "jti": uuid.uuid4().hex, "tv": int(token_version or 0), "iat": now}
        if settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES > 0:
            claims["exp"] = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
        return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    def revoke_token(self, db: Session, token: str) -> None:
        """Logout: this one token (device) stops working."""
        from app.db.models import RevokedTokenRecord
        try:
            claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
                                options={"verify_exp": False})
        except JWTError:
            return
        jti, sub = claims.get("jti"), claims.get("sub")
        if jti and sub and db.get(RevokedTokenRecord, jti) is None:
            db.add(RevokedTokenRecord(jti=jti, user_id=sub))
            db.commit()

    def end_all_sessions(self, db: Session, user: PlatformUserRecord) -> None:
        """Password change/reset or 'log out everywhere': every existing token stops working."""
        user.token_version = (user.token_version or 0) + 1
        db.commit()

    def _resolve_default_role(self, db: Session) -> RoleRecord | None:
        return db.query(RoleRecord).filter(RoleRecord.name == DEFAULT_ROLE_NAME).first()

    # ----------------------------------------------------------------
    # Token / OTP generators
    # ----------------------------------------------------------------
    def _generate_otp(self) -> str:
        return "".join(secrets.choice(string.digits) for _ in range(6))      # cryptographically random

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
        record_consent(db, new_user.user_id)

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
        from app.services.cache_service import cache_service
        if cache_service.hit(f"gisviz:otp-tries:{user.user_id}", 900) > 8:      # 6 digits: no brute force
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new code in a few minutes.")
        if user.verification_otp != otp:
            raise HTTPException(status_code=400, detail="Invalid OTP")
        if user.otp_expires_at and datetime.now(timezone.utc) > user.otp_expires_at:
            raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

        user.is_verified = 1
        user.is_active = 1
        user.verification_otp = None
        user.otp_expires_at = None
        db.commit()
        try:                                       # a verified email on a verified organisation's domain joins it
            from app.services import access_service
            access_service.sync_memberships(db, user)
        except Exception as e:
            print(f"[auth] organisation membership sync skipped: {e}")

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
        user.token_version = (user.token_version or 0) + 1      # log out every device
        db.commit()
        return {"status": "Password has been successfully reset"}


def record_consent(db: Session, user_id, documents=("terms", "privacy")) -> None:
    """GDPR: keep proof of which version of the terms / privacy policy a user accepted, and when."""
    from app.api.v0.endpoints.legal import LEGAL_VERSION
    from app.db.models import ConsentRecord
    for doc in documents:
        db.add(ConsentRecord(user_id=user_id, document=doc, version=LEGAL_VERSION))
    db.commit()


# ----------------------------------------------------------------
# Singleton used by all endpoint routers
# ----------------------------------------------------------------
auth_service = AuthenticationService()


# ----------------------------------------------------------------
# FastAPI dependencies (THESE WERE MISSING!)
# ----------------------------------------------------------------

_REASONS = {
    "missing": "Not signed in.",
    "expired": "Your session expired. Please sign in again.",
    "invalid": "Your session is no longer valid. Please sign in again.",
    "ended": "You were signed out (logout or password change). Please sign in again.",
}


def _user_from_token(token: Optional[str], db: Session) -> tuple[Optional[PlatformUserRecord], Optional[str]]:
    """(user, None) for a valid session token, else (None, reason)."""
    from app.db.models import RevokedTokenRecord
    if not token:
        return None, "missing"
    try:
        claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        return None, "expired"                     # issued with an expiry (JWT_ACCESS_TOKEN_EXPIRE_MINUTES > 0)
    except JWTError:
        return None, "invalid"                     # bad signature: JWT_SECRET_KEY changed, or a tampered token
    user_id = claims.get("sub")
    if not user_id:
        return None, "invalid"
    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if user is None:
        return None, "invalid"
    if int(claims.get("tv", 0)) != (user.token_version or 0):
        return None, "ended"                       # password changed / logged out everywhere
    jti = claims.get("jti")
    if jti and db.get(RevokedTokenRecord, jti) is not None:
        return None, "ended"                       # this device logged out
    return user, None


def get_current_authenticated_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_users_db),
) -> PlatformUserRecord:
    """Requires a valid session token — raises 401 if missing, invalid or logged out."""
    user, reason = _user_from_token(token, db)
    if user is None:
        if reason != "missing":
            print(f"[auth] 401 ({reason})", flush=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REASONS.get(reason, "Could not validate credentials"),
        )
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
    user, _reason = _user_from_token(token, db)
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


# Roles allowed to create/edit posts and use the dataset picker. Everyone else (viewer) is read-only.
PUBLISHING_ROLES = ["admin", "publisher", "editor", "support"]
require_publisher = RoleChecker(PUBLISHING_ROLES)

# Roles allowed to manage datasets (create, upload, activate, edit, delete) on /admin/datasets.
# Add "publisher" here (and in the frontend lib/roles.ts) to let publishers manage datasets too.
DATASET_MANAGER_ROLES = ["admin"]
require_dataset_manager = RoleChecker(DATASET_MANAGER_ROLES)