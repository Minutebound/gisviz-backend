import os
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class PlatformConfigurationSettings(BaseSettings):
    PROJECT_NAME: str = "Gisviz Enterprise API"
    VERSION: str = "3.1.0"
    API_V1_STR: str = "/api/v1"

    # ----- Auth / JWT -----
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 86400  # 60 * 24 * 60 = 60 days

    # ----- CORS -----
    BACKEND_CORS_ORIGINS: List[str] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    def _assemble_cors_origins(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # ----- Databases -----
    USERS_DATABASE_URL: str
    POSTS_DATABASE_URL: str
    # NEW: analytics warehouse (star schema, append-only snapshots)
    ANALYTICS_DATABASE_URL: str
    # NEW: admin operational store (audit log, control-panel actions)
    ADMIN_DATABASE_URL: str

    # ----- Cache -----
    REDIS_URL: str

    model_config = SettingsConfigDict(
        env_file=".env.backend", case_sensitive=True, extra="ignore"
    )


settings = PlatformConfigurationSettings()