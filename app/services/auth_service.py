import random
import string
import secrets
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone # Import timezone
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
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

DEFAULT_ROLE_NAME = "viewer"

class AuthenticationService:
    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        return pwd_context.hash(password)

    def create_access_token(self, subject: str) -> str:
        # Use timezone.utc
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
        to_encode = {"exp": expire, "sub": str(subject)}
        return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    def _resolve_default_role(self, db: Session) -> RoleRecord | None:
        return db.query(RoleRecord).filter(RoleRecord.name == DEFAULT_ROLE_NAME).first()

    def _generate_otp(self) -> str:
        return ''.join(random.choices(string.digits, k=6))

    def _generate_secure_token(self) -> str:
        return secrets.token_urlsafe(32)

    def simulate_send_email(self, email: str, subject: str, content: str):
        print(f"\n[{datetime.now(timezone.utc)}] EMAIL SENT TO: {email}")
        print(f"SUBJECT: {subject}")
        print(f"CONTENT:\n{content}\n")

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
            verification_otp=otp,
            otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        self.simulate_send_email(
            email=payload.email_address,
            subject="Verify your Account",
            content=f"Your verification code is: {otp}. It expires in 15 minutes."
        )
        
        return {"user": new_user, "dev_otp": otp}

    def send_email(self, to_email: str, subject: str, body: str):
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = settings.SMTP_USER
        msg['To'] = to_email

        try:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.login(settings.SMTP_USER, settings.SMTP_PASS)
                server.sendmail(settings.SMTP_USER, to_email, msg.as_string())
        except Exception as e:
            print(f"SMTP Error: {e}")
            raise HTTPException(status_code=500, detail="Failed to send email")
        
    def verify_email(self, db: Session, email_address: str, otp: str):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == email_address).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        if user.is_verified == 1:
            return {"status": "Already verified"}

        if user.verification_otp != otp:
            raise HTTPException(status_code=400, detail="Invalid OTP")

        # Now both sides are timezone-aware
        if user.otp_expires_at and datetime.now(timezone.utc) > user.otp_expires_at:
            raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

        user.is_verified = 1
        user.verification_otp = None
        user.otp_expires_at = None
        db.commit()
        return {"status": "Email verified successfully"}

    def initiate_password_reset(self, db: Session, email_address: str):
        user = db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == email_address).first()
        if not user:
            return {"status": "If the email exists, a reset link has been sent."}

        token = self._generate_secure_token()
        user.password_reset_token = token
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.commit()

        reset_link = f"http://localhost:3000/reset-password?token={token}"
        self.simulate_send_email(
            email=email_address,
            subject="Password Reset Request",
            content=f"Click here to reset your password: {reset_link}\nThis link expires in 30 minutes."
        )

        return {"status": "If the email exists, a reset link has been sent.", "dev_token": token}

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

auth_service = AuthenticationService()

def get_current_authenticated_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_users_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(PlatformUserRecord).filter(PlatformUserRecord.user_id == user_id).first()
    if user is None:
        raise credentials_exception
        
    # Enforce verification check
    if user.is_verified == 0:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account email not verified.")
        
    return user


class RoleChecker:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, user: PlatformUserRecord = Depends(get_current_authenticated_user)):
        # Safely determine the role name, defaulting to viewer if none is set
        role_name = user.role.name if user.role else "viewer"
        
        # Admin bypasses all role checks
        if role_name == "admin":
            return user
            
        if role_name not in self.allowed_roles:
            from fastapi import status # Ensure status is available
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation not permitted. Requires one of: {self.allowed_roles}"
            )
        return user