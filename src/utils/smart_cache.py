"""
SmartCache — Cache com TTL, Redis como backend distribuído e fallback para memória.
Suporta invalidação por ETag/Last-Modified para fontes que suportam HTTP caching.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, UTC
from typing import Any

logger = logging.getLogger(__name__)


class SmartCache:
    """
    Cache com fallback Redis → memória.
    TTL por estratégia (ex: GitHub: 1h, notícias: 15min, papers: 24h).
    """

    TTL_STRATEGIES = {
        "github": 3600,        # 1 hora
        "news": 900,           # 15 minutos
        "papers": 86400,       # 24 horas
        "reddit": 1800,        # 30 minutos
        "hackernews": 1800,    # 30 minutos
        "arxiv": 86400,        # 24 horas
        "stackoverflow": 3600, # 1 hora
        "default": 3600,       # 1 hora
    }

    def __init__(self, redis_url: str | None = None):
        self.redis = None
        self.memory: dict[str, dict] = {}
        if redis_url:
            try:
                import redis.asyncio as redis_lib
                self.redis = redis_lib.from_url(redis_url, decode_responses=True)
                logger.info("SmartCache: Redis conectado")
            except Exception as e:
                logger.warning(f"Redis indisponível, usando memória: {e}")

    async def get(
        self,
        key: str,
        check_etag: bool = False,
        etag_url: str | None = None
    ) -> Any | None:
        cached = await self._get_raw(key)
        if cached is None:
            return None

        expires = datetime.fromisoformat(cached["expires"])
        if expires < datetime.now(UTC):
            await self.delete(key)
            return None

        if check_etag and etag_url and cached.get("etag"):
            current_etag = await self._fetch_etag(etag_url)
            if current_etag and current_etag != cached["etag"]:
                logger.info(f"Cache invalidado por ETag: {key}")
                await self.delete(key)
                return None

        return cached["value"]

    async def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
        source_type: str = "default",
        etag: str | None = None,
    ) -> None:
        ttl = ttl_seconds or self.TTL_STRATEGIES.get(source_type, self.TTL_STRATEGIES["default"])
        expires = datetime.now(UTC) + timedelta(seconds=ttl)
        data = {
            "value": value,
            "expires": expires.isoformat(),
            "etag": etag,
            "content_hash": self._hash(value),
        }
        await self._set_raw(key, data, ttl)

    async def delete(self, key: str) -> None:
        if self.redis:
            try:
                await self.redis.delete(f"sra:{key}")
                return
            except Exception:
                pass
        self.memory.pop(key, None)

    async def _get_raw(self, key: str) -> dict | None:
        if self.redis:
            try:
                raw = await self.redis.get(f"sra:{key}")
                return json.loads(raw) if raw else None
            except Exception as e:
                logger.warning(f"Redis get falhou: {e}")
        return self.memory.get(key)

    async def _set_raw(self, key: str, data: dict, ttl: int) -> None:
        if self.redis:
            try:
                await self.redis.setex(f"sra:{key}", ttl, json.dumps(data, default=str))
                return
            except Exception as e:
                logger.warning(f"Redis set falhou, usando memória: {e}")
        self.memory[key] = data

    def make_key(self, prefix: str, query: str) -> str:
        """Gera uma chave de cache determinística a partir de prefix + query."""
        return hashlib.sha256(f"{prefix}:{query}".encode()).hexdigest()[:24]

    def _hash(self, value: Any) -> str:
        content = json.dumps(value, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def _fetch_etag(self, url: str) -> str | None:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.head(url)
                return r.headers.get("etag") or r.headers.get("last-modified")
        except Exception:
            return None
