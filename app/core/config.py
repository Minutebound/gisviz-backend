import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class PlatformConfigurationSettings(BaseSettings):
    PROJECT_NAME: str = "gisviz Enterprise API"
    VERSION: str = "2.0.0"
    API_V1_STR: str = "/api/v1"
    
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int
    
    BACKEND_CORS_ORIGINS: List[str]
    
    AUTH_DATABASE_URL: str
    SPATIAL_DATABASE_URL: str
    REDIS_URL: str

    model_config = SettingsConfigDict(env_file=".env.backend", case_sensitive=True, extra="ignore")

settings = PlatformConfigurationSettings()