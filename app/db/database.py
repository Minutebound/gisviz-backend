from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings


# Enterprise Engine Configurations (Prevents VPS memory/connection exhaustion)
engine_settings = {
    "pool_pre_ping": True,
    "pool_size": 20,
    "max_overflow": 10,
    "pool_recycle": 1800,  # Recycles connections after 30 minutes
}

# ============================================================
# 1. USERS Database
# ============================================================
users_engine = create_engine(settings.USERS_DATABASE_URL, pool_pre_ping=True,
                              pool_size=20, max_overflow=10, pool_recycle=1800)
UsersSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=users_engine)
UsersBase = declarative_base()
 
# ============================================================
# 2. POSTS Database
# ============================================================
posts_engine = create_engine(settings.POSTS_DATABASE_URL, pool_pre_ping=True,
                              pool_size=20, max_overflow=10, pool_recycle=1800)
PostsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=posts_engine)
PostsBase = declarative_base()
 
# ============================================================
# 3. ANALYTICS Database  (star-schema warehouse, append-only)
# ============================================================
analytics_engine = create_engine(settings.ANALYTICS_DATABASE_URL, pool_pre_ping=True,
                                  pool_size=5, max_overflow=5, pool_recycle=1800)
AnalyticsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=analytics_engine)
AnalyticsBase = declarative_base()
 
# ============================================================
# 4. ADMIN Database  (audit log, control-panel operational data)
# ============================================================
admin_engine = create_engine(settings.ADMIN_DATABASE_URL, pool_pre_ping=True,
                              pool_size=5, max_overflow=5, pool_recycle=1800)
AdminSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=admin_engine)
AdminBase = declarative_base()
 
 
# FastAPI dependency helpers
def get_users_db():
    db = UsersSessionLocal()
    try:
        yield db
    finally:
        db.close()
 
 
def get_posts_db():
    db = PostsSessionLocal()
    try:
        yield db
    finally:
        db.close()
 
 
def get_analytics_db():
    db = AnalyticsSessionLocal()
    try:
        yield db
    finally:
        db.close()
 
 
def get_admin_db():
    db = AdminSessionLocal()
    try:
        yield db
    finally:
        db.close()