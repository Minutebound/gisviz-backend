import json
import hashlib
from typing import Optional, List, Any
import redis

from app.core.config import settings


class CacheService:
    """Redis layer. Holds ONLY rebuildable, derived data:
      - search results  (read-through cache, TTL-bounded)
      - trending        (sorted set scored by engagement)
      - live counters   (INCR in Redis, periodically flushed to Postgres)
    Postgres always remains the source of truth.
    """

    SEARCH_PREFIX = "search:"
    TRENDING_KEY = "trending:pubs"
    LIKE_COUNTER_PREFIX = "counter:like:"
    COMMENT_COUNTER_PREFIX = "counter:comment:"

    def __init__(self):
        # decode_responses keeps everything as str rather than bytes
        self.client = redis.from_url(settings.REDIS_URL, decode_responses=True)

    # ---------- helpers ----------
    @staticmethod
    def _hash_query(query: str, skip: int, limit: int) -> str:
        raw = f"{query.strip().lower()}|{skip}|{limit}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    # ---------- search cache ----------
    def get_search_results(self, query: str, skip: int, limit: int) -> Optional[List[Any]]:
        key = self.SEARCH_PREFIX + self._hash_query(query, skip, limit)
        try:
            cached = self.client.get(key)
            if cached:
                return json.loads(cached)
        except redis.RedisError:
            pass  # cache is best-effort; never block the request
        return None

    def set_search_results(self, query: str, skip: int, limit: int, results: List[Any]) -> None:
        key = self.SEARCH_PREFIX + self._hash_query(query, skip, limit)
        try:
            self.client.set(
                key,
                json.dumps(results, default=str),
                ex=settings.SEARCH_CACHE_TTL_SECONDS,
            )
        except redis.RedisError:
            pass

    def invalidate_search(self) -> None:
        """Drop all cached searches — call after a new publication is created
        so fresh content shows up immediately rather than waiting for TTL."""
        try:
            for key in self.client.scan_iter(match=self.SEARCH_PREFIX + "*", count=500):
                self.client.delete(key)
        except redis.RedisError:
            pass

    # ---------- trending ----------
    def bump_trending(self, publication_id: str, weight: float = 1.0) -> None:
        try:
            self.client.zincrby(self.TRENDING_KEY, weight, publication_id)
        except redis.RedisError:
            pass

    def top_trending(self, n: int = 10) -> List[str]:
        try:
            return self.client.zrevrange(self.TRENDING_KEY, 0, n - 1)
        except redis.RedisError:
            return []

    # ---------- live counters ----------
    def incr_like(self, publication_id: str, amount: int = 1) -> int:
        try:
            return self.client.incrby(self.LIKE_COUNTER_PREFIX + publication_id, amount)
        except redis.RedisError:
            return 0

    def incr_comment(self, publication_id: str, amount: int = 1) -> int:
        try:
            return self.client.incrby(self.COMMENT_COUNTER_PREFIX + publication_id, amount)
        except redis.RedisError:
            return 0


cache_service = CacheService()