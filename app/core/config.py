import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class PlatformConfigurationSettings(BaseSettings):
    PROJECT_NAME: str = "Gisviz Enterprise API"
    VERSION: str = "3.0.0"
    API_V1_STR: str = "/api/v1"

    # ----- Auth / JWT -----
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 43800

    # ----- CORS -----
    BACKEND_CORS_ORIGINS: List[str] = []

    # ----- Databases -----
    # Renamed from AUTH_DATABASE_URL  -> USERS_DATABASE_URL
    # Renamed from SPATIAL_DATABASE_URL -> POSTS_DATABASE_URL  (still PostGIS-backed)
    USERS_DATABASE_URL: str
    POSTS_DATABASE_URL: str

    # ----- Cache -----
    REDIS_URL: str

    model_config = SettingsConfigDict(
        env_file=".env.backend", case_sensitive=True, extra="ignore"
    )


settings = PlatformConfigurationSettings()