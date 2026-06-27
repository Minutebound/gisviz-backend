import json
import logging
import redis
from app.core.config import settings

logger = logging.getLogger(__name__)

class CacheService:
    def __init__(self):
        try:
            self.client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
            self.client.ping()
        except Exception as e:
            logger.warning(f"Redis cache unavailable, gracefully falling back to direct DB queries. Error: {e}")
            self.client = None

    def get(self, key: str):
        if not self.client: return None
        try:
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception:
            return None

    def set(self, key: str, value: dict, expire: int = 300):
        if not self.client: return
        try:
            self.client.set(key, json.dumps(value), ex=expire)
        except Exception:
            pass
            
    def delete_pattern(self, pattern: str):
        if not self.client: return
        try:
            for key in self.client.scan_iter(pattern):
                self.client.delete(key)
        except Exception:
            pass

cache_service = CacheService()