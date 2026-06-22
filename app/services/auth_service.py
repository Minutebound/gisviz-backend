from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from fastapi import HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from app.core.config import settings
from app.db.models import PlatformUserRecord
from app.schemas.user_schema import UserRegistrationPayload

# -> CHANGED: Import the new auth DB dependency
from app.db.database import get_auth_db 

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

class AuthenticationService:
    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        return pwd_context.hash(password)

    def create_access_token(self, subject: str) -> str:
        expire = datetime.utcnow() + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
        to_encode = {"exp": expire, "sub": str(subject)}
        return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    def register_new_user(self, db: Session, payload: UserRegistrationPayload) -> PlatformUserRecord:
        if db.query(PlatformUserRecord).filter(PlatformUserRecord.email_address == payload.email_address).first():
            raise HTTPException(status_code=400, detail="Email already registered")
            
        new_user = PlatformUserRecord(
            user_handle=payload.user_handle,
            email_address=payload.email_address,
            hashed_security_password=self.get_password_hash(payload.plaintext_password),
            avatar_storage_url="https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=150"
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return new_user

auth_service = AuthenticationService()

# -> CHANGED: Depends(get_auth_db)
def get_current_authenticated_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_auth_db)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
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
    return user