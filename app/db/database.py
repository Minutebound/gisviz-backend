from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

# Enterprise Engine Configurations (Prevents VPS memory/connection exhaustion)
engine_settings = {
    "pool_pre_ping": True,
    "pool_size": 20,
    "max_overflow": 10,
    "pool_recycle": 1800, # Recycles connections after 30 minutes
}

# 1. Auth Database Setup
auth_engine = create_engine(settings.AUTH_DATABASE_URL, **engine_settings)
AuthSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=auth_engine)
AuthBase = declarative_base() 

# 2. Spatial Database Setup
spatial_engine = create_engine(settings.SPATIAL_DATABASE_URL, **engine_settings)
SpatialSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=spatial_engine)
SpatialBase = declarative_base() 

# Dependency Injections
def get_auth_db():
    db = AuthSessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_spatial_db():
    db = SpatialSessionLocal()
    try:
        yield db
    finally:
        db.close()