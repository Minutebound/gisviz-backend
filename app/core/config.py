import os
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Literal


class PlatformConfigurationSettings(BaseSettings):
    PROJECT_NAME: str = "Gisviz Enterprise API"
    VERSION: str = "3.1.0"
    API_V1_STR: str = "/api/v1"

    # ── Environment gate ──────────────────────────────────────────────
    # DO NOT set this in .env.backend.
    # It is baked into the Docker image by the Dockerfile:
    #   target: dev  → ENV APP_ENV=development
    #   target: prod → ENV APP_ENV=production
    #
    # This makes the auth behaviour (real email vs console print,
    # dev_otp in response vs hidden) automatically follow the build
    # target — they can never be out of sync.
    #
    # The default "development" means running bare uvicorn outside
    # Docker (e.g. local venv) also gets safe dev behaviour.
    APP_ENV: Literal["development", "production"] = "development"

    # ── Auth / JWT ────────────────────────────────────────────────────
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int

    # ── CORS ──────────────────────────────────────────────────────────
    BACKEND_CORS_ORIGINS: List[str] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    def _assemble_cors_origins(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # ── Databases ─────────────────────────────────────────────────────
    USERS_DATABASE_URL: str
    POSTS_DATABASE_URL: str
    ANALYTICS_DATABASE_URL: str
    ADMIN_DATABASE_URL: str

    # ── Cache ─────────────────────────────────────────────────────────
    REDIS_URL: str

    # ── Email / SMTP ──────────────────────────────────────────────────
    # Leave blank in development — _email() routes to simulate_send_email()
    # which only prints to console and never touches these values.
    #
    # In production set all four in .env.backend on the VPS.
    # IONOS SMTP example:
    #   SMTP_HOST=smtp.ionos.com
    #   SMTP_PORT=465
    #   SMTP_USER=noreply@yourdomain.com
    #   SMTP_PASS=your-smtp-password
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASS: str = ""

    # ── Frontend URL ──────────────────────────────────────────────────
    # Used only in initiate_password_reset() to build the clickable
    # reset link that goes inside the email.
    #
    # This is NOT a secret — it is your public domain name.
    # Set it in .env.backend so reset links point to the right place:
    #   dev     → http://localhost:3001       (default, works out of the box)
    #   staging → https://staging.yourdomain.com
    #   prod    → https://yourdomain.com
    FRONTEND_URL: str 

    model_config = SettingsConfigDict(
        env_file=".env.backend",
        case_sensitive=True,
        extra="ignore",
    )


settings = PlatformConfigurationSettings()