
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

# ============================================================
# 1. USERS Database  (identity, roles, social graph)
#    Renamed from "auth" -> "users"
# ============================================================
users_engine = create_engine(settings.USERS_DATABASE_URL, pool_pre_ping=True)
UsersSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=users_engine)
UsersBase = declarative_base()

# ============================================================
# 2. POSTS Database  (publications, likes, comments, categories)
#    Renamed from "spatial" -> "posts"  (still PostGIS-enabled)
# ============================================================
posts_engine = create_engine(settings.POSTS_DATABASE_URL, pool_pre_ping=True)
PostsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=posts_engine)
PostsBase = declarative_base()


# ------------------------------------------------------------
# FastAPI dependencies
# ------------------------------------------------------------
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