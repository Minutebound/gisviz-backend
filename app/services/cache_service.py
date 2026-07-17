import json
from typing import Any, Optional

import redis

from app.core.config import settings


class CacheService:
    """
    Lightweight synchronous Redis wrapper.

    Used only for data that is written by background schedulers and read
    by API endpoints (e.g. trending_posts). All fastapi-cache2 caching
    (@cache decorator) is handled separately via FastAPICache.init() in
    main.py — this class is NOT a replacement for that.

    Falls back to a no-op when Redis is unreachable so the API keeps
    running in environments where Redis is temporarily unavailable.
    """

    def __init__(self) -> None:
        try:
            self._client: Optional[redis.Redis] = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
            )
            # Verify the connection is alive
            self._client.ping()
        except Exception as e:
            print(f"[CacheService] Redis unavailable — running without sync cache: {e}")
            self._client = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        """Return the deserialised value for *key*, or None on miss / error."""
        if self._client is None:
            return None
        try:
            raw = self._client.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            print(f"[CacheService] get({key!r}) failed: {e}")
            return None

    def set(self, key: str, value: Any, ttl_seconds: int = 120) -> bool:
        """
        Serialise *value* as JSON and store it with a TTL.
        Returns True on success, False on error.
        """
        if self._client is None:
            return False
        try:
            self._client.set(key, json.dumps(value), ex=ttl_seconds)
            return True
        except Exception as e:
            print(f"[CacheService] set({key!r}) failed: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True on success, False on error."""
        if self._client is None:
            return False
        try:
            self._client.delete(key)
            return True
        except Exception as e:
            print(f"[CacheService] delete({key!r}) failed: {e}")
            return False

    def delete_pattern(self, pattern: str) -> int:
        """
        Delete all keys matching a glob pattern (e.g. 'gisviz-cache:search:global:*').
        Returns the number of keys deleted, or 0 on error / no match.
        Uses SCAN so it is safe on large keyspaces (no blocking KEYS call).
        """
        if self._client is None:
            return 0
        try:
            deleted = 0
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted += self._client.delete(*keys)
                if cursor == 0:
                    break
            return deleted
        except Exception as e:
            print(f"[CacheService] delete_pattern({pattern!r}) failed: {e}")
            return 0

    def is_healthy(self) -> bool:
        """Return True if the Redis connection is alive."""
        if self._client is None:
            return False
        try:
            return self._client.ping()
        except Exception:
            return False


# Singleton — imported by endpoints as `from app.services.cache_service import cache_service`
cache_service = CacheService()