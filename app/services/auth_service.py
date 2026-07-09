import random
import string
import secrets
import smtplib
from email.mime.text import MIMEText
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

# auto_error=False means FastAPI returns None (not 401) when the
# Authorization header is missing — required for optional auth on
# public endpoints that annotate per-user state when logged in.
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
    # JWT
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

    # ----------------------------------------------------------------
    # Role resolution
    # ----------------------------------------------------------------

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
    # Environment helpers — driven by APP_ENV baked into the Docker image.
    #
    #   target: dev  → ENV APP_ENV=development in Dockerfile
    #   target: prod → ENV APP_ENV=production  in Dockerfile
    #
    # This means the behaviour is tied to the build target, not to any
    # value in .env.backend, so they can never be out of sync.
    # ----------------------------------------------------------------

    @property
    def _is_prod(self) -> bool:
        """True only when the prod Docker image is running."""
        return settings.APP_ENV == "production"

    def _email(self, to_email: str, subject: str, body: str) -> None:
        """
        Central email router.
        - dev / staging  → simulate_send_email() (prints to console, no SMTP needed)
        - production     → send_email()           (real SMTP, raises 500 on failure)
        """
        if self._is_prod:
            self.send_email(to_email, subject, body)
        else:
            self.simulate_send_email(to_email, subject, body)

    def _expose_dev_field(self, value: str) -> Optional[str]:
        """
        Returns value in dev/staging so the UI can display it.
        Returns None in production so it is never included in any response.
        Callers must check for None before adding the key to a response dict.
        """
        return None if self._is_prod else value

    # ----------------------------------------------------------------
    # Email backends
    # ----------------------------------------------------------------

    def simulate_send_email(self, email: str, subject: str, content: str) -> None:
        """Dev-only — prints the email to stdout so you can read OTPs in the logs."""
        print(f"\n[{datetime.now(timezone.utc)}] EMAIL TO: {email}")
        print(f"SUBJECT: {subject}")
        print(f"CONTENT:\n{content}\n")

    def send_email(self, to_email: str, subject: str, body: str) -> None:
        """Production — real SMTP send. Raises HTTP 500 on failure."""
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_USER
        msg["To"] = to_email
        try:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.login(settings.SMTP_USER, settings.SMTP_PASS)
                server.sendmail(settings.SMTP_USER, to_email, msg.as_string())
        except Exception as e:
            print(f"SMTP Error: {e}")
            raise HTTPException(status_code=500, detail="Failed to send email")

    # ----------------------------------------------------------------
    # Auth flows
    # ----------------------------------------------------------------

    def register_new_user(self, db: Session, payload: UserRegistrationPayload):
        if db.query(PlatformUserRecord).filter(
            PlatformUserRecord.email_address == payload.email_address
        ).first():
            raise HTTPException(status_code=400, detail="Email already registered")

        if db.query(PlatformUserRecord).filter(
            PlatformUserRecord.user_handle == payload.user_handle
        ).first():
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

        # dev  → prints OTP to console
        # prod → sends real email, OTP never leaves the server
        self._email(
            to_email=payload.email_address,
            subject="Verify your Account",
            body=f"Your verification code is: {otp}. It expires in 15 minutes.",
        )

        # dev_otp is None in prod — the endpoint checks before adding it to the response
        return {"user": new_user, "dev_otp": self._expose_dev_field(otp)}

    def verify_email(self, db: Session, email_address: str, otp: str):
        user = db.query(PlatformUserRecord).filter(
            PlatformUserRecord.email_address == email_address
        ).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.is_verified == 1 and user.is_active == 1:
            return {"status": "Already verified"}

        if user.verification_otp != otp:
            raise HTTPException(status_code=400, detail="Invalid OTP")

        if user.otp_expires_at and datetime.now(timezone.utc) > user.otp_expires_at:
            raise HTTPException(
                status_code=400,
                detail="OTP has expired. Please request a new one.",
            )

        user.is_verified = 1
        user.is_active = 1
        user.verification_otp = None
        user.otp_expires_at = None
        db.commit()

        return {"status": "Email verified successfully", "reactivated": True}

    def initiate_password_reset(self, db: Session, email_address: str):
        user = db.query(PlatformUserRecord).filter(
            PlatformUserRecord.email_address == email_address
        ).first()

        # Always return the same message — prevents user enumeration
        if not user:
            return {"status": "If the email exists, a reset link has been sent."}

        token = self._generate_secure_token()
        user.password_reset_token = token
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.commit()

        # dev  → uses localhost:3000 (from FRONTEND_URL default), prints link to console
        # prod → uses https://yourdomain.com (from FRONTEND_URL in .env.backend), sends real email
        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        self._email(
            to_email=email_address,
            subject="Password Reset Request",
            body=(
                f"Click here to reset your password:\n{reset_link}\n\n"
                "This link expires in 30 minutes.\n"
                "If you did not request this, you can safely ignore this email."
            ),
        )

        return {
            "status": "If the email exists, a reset link has been sent.",
            # dev_token is None in prod — endpoint checks before adding to response
            "dev_token": self._expose_dev_field(token),
        }

    def execute_password_reset(self, db: Session, token: str, new_password: str):
        user = db.query(PlatformUserRecord).filter(
            PlatformUserRecord.password_reset_token == token
        ).first()
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
# FastAPI dependencies
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