import hashlib
import json
import logging
import os
import gzip
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Cache:
    TTL_STRATEGIES = {
        "github": 3600,        # 1 hora
        "news": 900,           # 15 minutos
        "papers": 86400,       # 24 horas
        "reddit": 1800,        # 30 minutos
        "hackernews": 1800,    # 30 minutos
        "arxiv": 86400,        # 24 horas
        "stackoverflow": 3600, # 1 hora
        "rss": 1800,           # 30 minutos
        "default": 3600,       # 1 hora
    }

    def __init__(self, cache_dir: str = "./.cache", redis_url: str | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.redis = None
        self.memory: dict[str, dict] = {}
        
        if redis_url:
            try:
                import redis.asyncio as redis_lib
                self.redis = redis_lib.from_url(redis_url, decode_responses=True)
                logger.info(f"Cache: Redis conectado em {redis_url}")
            except Exception as e:
                logger.warning(f"Redis indisponível, usando fallback local: {e}")

    def _filename(self, prefix: str, query: str | None = None) -> str:
        if query is None:
            # SmartCache key única
            return f"smart_{prefix}.json.gz"
        hash_key = hashlib.sha256(f"{prefix}:{query}".encode()).hexdigest()
        return f"{prefix}_{hash_key}.json.gz"

    async def get(
        self,
        prefix_or_key: str,
        query: str | None = None,
        check_etag: bool = False,
        etag_url: str | None = None
    ) -> Any | None:
        if query is None:
            # SmartCache (key única)
            key = prefix_or_key
            filename = self._filename(prefix_or_key)
        else:
            # Cache antigo (prefix, query)
            key = f"{prefix_or_key}:{query}"
            filename = self._filename(prefix_or_key, query)

        # 1. Tentar Redis
        if self.redis:
            try:
                raw = await self.redis.get(f"sra:{key}")
                if raw:
                    cached = json.loads(raw)
                    expires = datetime.fromisoformat(cached["expires"])
                    if expires > datetime.now(UTC):
                        return cached["value"]
                    else:
                        await self.delete(prefix_or_key, query)
                        return None
            except Exception as e:
                logger.warning(f"Redis get falhou: {e}")

        # 2. Tentar Memória
        cached = self.memory.get(key)
        if cached:
            expires = datetime.fromisoformat(cached["expires"])
            # Suporta tanto isoformat aware (UTC) quanto naive (UTC nos testes antigos)
            expires_val = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
            now_val = datetime.now(UTC) if expires_val.tzinfo is not None else datetime.now()
            if expires_val > now_val:
                return cached["value"]
            else:
                await self.delete(prefix_or_key, query)
                return None

        # 3. Tentar Disco Local (Gzip)
        path = self.cache_dir / filename
        if not path.exists():
            return None
            
        try:
            import asyncio
            data_bytes = await asyncio.to_thread(path.read_bytes)
            decompressed = gzip.decompress(data_bytes).decode("utf-8")
            data = json.loads(decompressed)
            
            # Valida expiração
            expires = datetime.fromisoformat(data["expires"])
            expires_val = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
            now_val = datetime.now(UTC) if expires_val.tzinfo is not None else datetime.now()
            if expires_val < now_val:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                return None
                
            # Atualiza cache em memória
            self.memory[key] = data
            return data["value"]
        except Exception as e:
            logger.warning(f"Erro ao ler cache local de {path}: {e}")
            return None

    async def set(
        self,
        prefix_or_key: str,
        query_or_value: Any,
        value: Any = None,
        ttl_seconds: int | None = None,
        source_type: str = "default",
        etag: str | None = None,
    ) -> None:
        if value is None:
            # SmartCache (key, value)
            key = prefix_or_key
            value_to_cache = query_or_value
            prefix = source_type
            filename = self._filename(prefix_or_key)
        else:
            # Cache antigo (prefix, query, value)
            key = f"{prefix_or_key}:{query_or_value}"
            value_to_cache = value
            prefix = prefix_or_key
            filename = self._filename(prefix_or_key, query_or_value)

        ttl = ttl_seconds or self.TTL_STRATEGIES.get(prefix, self.TTL_STRATEGIES["default"])
        expires = datetime.now(UTC) + timedelta(seconds=ttl)
        
        data = {
            "value": value_to_cache,
            "expires": expires.isoformat(),
            "cached_at": datetime.now(UTC).isoformat()
        }

        # 1. Salvar no Redis
        if self.redis:
            try:
                await self.redis.setex(f"sra:{key}", ttl, json.dumps(data, default=str))
            except Exception as e:
                logger.warning(f"Redis set falhou: {e}")

        # 2. Salvar na Memória
        self.memory[key] = data

        # 3. Salvar no Disco Local (Gzip)
        path = self.cache_dir / filename
        try:
            import asyncio
            serialized = json.dumps(data, default=str, indent=2)
            compressed = gzip.compress(serialized.encode("utf-8"))
            await asyncio.to_thread(path.write_bytes, compressed)
        except Exception as e:
            logger.warning(f"Erro ao escrever cache local em {path}: {e}")

    async def delete(self, prefix_or_key: str, query: str | None = None) -> None:
        if query is None:
            key = prefix_or_key
            filename = self._filename(prefix_or_key)
        else:
            key = f"{prefix_or_key}:{query}"
            filename = self._filename(prefix_or_key, query)

        if self.redis:
            try:
                await self.redis.delete(f"sra:{key}")
            except Exception:
                pass
        self.memory.pop(key, None)
        
        path = self.cache_dir / filename
        if path.exists():
            import asyncio
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception:
                pass

    async def invalidate(self, prefix: str) -> None:
        # Invalida memória
        keys_to_del = [k for k in self.memory if k.startswith(f"{prefix}:") or k == prefix]
        for k in keys_to_del:
            self.memory.pop(k, None)
            
        # Invalida Redis se disponível
        if self.redis:
            try:
                keys = await self.redis.keys(f"sra:{prefix}:*")
                if keys:
                    await self.redis.delete(*keys)
                await self.redis.delete(f"sra:{prefix}")
            except Exception:
                pass

        # Invalida arquivos no disco local
        import asyncio
        def _unlink_glob():
            # Remove arquivos com padrão prefix_*.json.gz ou smart_prefix.json.gz
            for f in self.cache_dir.glob(f"{prefix}_*.json.gz"):
                try:
                    f.unlink()
                except Exception:
                    pass
            smart_f = self.cache_dir / f"smart_{prefix}.json.gz"
            if smart_f.exists():
                try:
                    smart_f.unlink()
                except Exception:
                    pass
        await asyncio.to_thread(_unlink_glob)

    def make_key(self, prefix: str, query: str) -> str:
        """Gera uma chave de cache determinística."""
        return hashlib.sha256(f"{prefix}:{query}".encode()).hexdigest()[:24]
