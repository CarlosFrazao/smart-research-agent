"""Cache assíncrono (Redis → memória → disco) com cache semântico por
similaridade de embeddings, TTL adaptativo por fonte e reaquecimento de
queries populares.

Mantém 100% de compatibilidade com a API anterior — todas as chamadas
existentes (SearchService, testes, SmartCache no orchestrator) continuam
funcionando sem alteração de assinatura.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.utils.semantic_cache import DEFAULT_SIMILARITY_THRESHOLD, SemanticCache

logger = logging.getLogger(__name__)


class Cache:
    """Cache multi-camada com semântica, TTL adaptativo e reaquecimento.

    Duas formas de uso, ambas suportadas na mesma API (compatibilidade legada):

    1. Modo "prefix + query" (busca em texto livre, usado por SearchService):
        await cache.set("github", "melhores CRMs open source", resultado)
        valor = await cache.get("github", "melhores CRMs open source")

    2. Modo "chave única" (SmartCache, usado pelo orchestrator):
        await cache.set("minha_chave", valor, ttl_seconds=60)
        valor = await cache.get("minha_chave")

    Apenas o modo (1) participa do cache semântico, pois é o único com texto
    de query livre o suficiente para ter variações semanticamente equivalentes.
    """

    TTL_STRATEGIES: dict[str, int] = {
        "github": 86400,  # 24h — repositórios/metadados mudam devagar
        "news": 900,  # 15 min — conteúdo de alta volatilidade
        "papers": 86400,  # 24h
        "reddit": 3600,  # 1h — threads mudam rápido (comentários/votos)
        "hackernews": 1800,  # 30 min
        "arxiv": 86400,  # 24h
        "stackoverflow": 3600,  # 1h
        "rss": 1800,  # 30 min
        "default": 3600,  # 1h
    }
    MAX_WARM_TTL = 7 * 86400  # teto de 7 dias, mesmo para queries muito populares

    def __init__(
        self,
        cache_dir: str = "./.cache",
        redis_url: str | None = None,
        semantic_enabled: bool = True,
        semantic_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        warm_threshold: int = 3,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.redis = None
        self.memory: dict[str, dict] = {}
        self.enabled = True

        # Cache semântico: resolve hits por similaridade (>= threshold) quando
        # não há match exato de chave. Opcional — degrada graciosamente se
        # sentence-transformers não estiver instalado.
        self.semantic = SemanticCache(threshold=semantic_threshold) if semantic_enabled else None

        # Cache warming: contabiliza acessos por chave para priorizar
        # reaquecimento e estender o TTL de queries populares.
        self.warm_threshold = warm_threshold
        self._access_counts: dict[str, int] = {}
        self._key_meta: dict[str, dict[str, Any]] = {}  # key -> {filename, prefix, ttl, query}

        if redis_url:
            try:
                import redis.asyncio as redis_lib

                self.redis = redis_lib.from_url(redis_url, decode_responses=True)
                logger.info(f"Cache: Redis conectado em {redis_url}")
            except Exception as e:
                logger.warning(f"Redis indisponível, usando fallback local: {e}")

    # ── Chaves ────────────────────────────────────────────────────────────────

    def _filename(self, prefix: str, query: str | None = None) -> str:
        if query is None:
            return f"smart_{prefix}.json.gz"
        hash_key = hashlib.sha256(f"{prefix}:{query}".encode()).hexdigest()
        return f"{prefix}_{hash_key}.json.gz"

    def make_key(self, prefix: str, query: str) -> str:
        """Gera uma chave de cache determinística."""
        return hashlib.sha256(f"{prefix}:{query}".encode()).hexdigest()[:24]

    def _resolve_ttl_bucket(self, prefix: str, query_hint: str | None) -> str:
        """Infere a categoria real de TTL quando `prefix` é genérico.

        SearchService grava sempre com prefix literal "search" e embute a fonte
        real dentro da própria chave (ex: "github:consulta"). Sem essa inferência
        o TTL adaptativo por fonte nunca chegava a ser aplicado na prática — todo
        resultado de busca caía no bucket "default" (1h), independente da fonte.
        """
        if prefix in self.TTL_STRATEGIES:
            return prefix
        if query_hint and ":" in query_hint:
            candidate = query_hint.split(":", 1)[0]
            if candidate in self.TTL_STRATEGIES:
                return candidate
        return prefix

    # ── Leitura/escrita de baixo nível (compartilhada entre get/set/semântico/warming) ──

    async def _read_tiers(self, key: str, filename: str) -> Any | None:
        """Consulta Redis → memória → disco, nessa ordem, para uma (key, filename) resolvidas."""
        if self.redis:
            try:
                raw = await self.redis.get(f"sra:{key}")
                if raw:
                    cached = json.loads(raw)
                    expires = datetime.fromisoformat(cached["expires"])
                    if expires > datetime.now(UTC):
                        return cached["value"]
                    await self._delete_raw(key, filename)
                    return None
            except Exception as e:
                logger.warning(f"Redis get falhou: {e}")

        cached = self.memory.get(key)
        if cached:
            expires = datetime.fromisoformat(cached["expires"])
            expires_val = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
            now_val = datetime.now(UTC) if expires_val.tzinfo is not None else datetime.now()
            if expires_val > now_val:
                return cached["value"]
            await self._delete_raw(key, filename)
            return None

        path = self.cache_dir / filename
        if not path.exists():
            return None

        try:
            data_bytes = await asyncio.to_thread(path.read_bytes)
            decompressed = gzip.decompress(data_bytes).decode("utf-8")
            data = json.loads(decompressed)

            expires = datetime.fromisoformat(data["expires"])
            expires_val = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
            now_val = datetime.now(UTC) if expires_val.tzinfo is not None else datetime.now()
            if expires_val < now_val:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                return None

            self.memory[key] = data
            return data["value"]
        except Exception as e:
            logger.warning(f"Erro ao ler cache local de {path}: {e}")
            return None

    async def _write_tiers(self, key: str, filename: str, value: Any, ttl: int) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=ttl)
        data = {
            "value": value,
            "expires": expires.isoformat(),
            "cached_at": datetime.now(UTC).isoformat(),
        }

        if self.redis:
            try:
                await self.redis.setex(f"sra:{key}", ttl, json.dumps(data, default=str))
            except Exception as e:
                logger.warning(f"Redis set falhou: {e}")

        self.memory[key] = data

        path = self.cache_dir / filename
        try:
            serialized = json.dumps(data, default=str, indent=2)
            compressed = gzip.compress(serialized.encode("utf-8"))
            await asyncio.to_thread(path.write_bytes, compressed)
        except Exception as e:
            logger.warning(f"Erro ao escrever cache local em {path}: {e}")

    async def _delete_raw(self, key: str, filename: str) -> None:
        if self.redis:
            try:
                await self.redis.delete(f"sra:{key}")
            except Exception:
                pass
        self.memory.pop(key, None)
        path = self.cache_dir / filename
        if path.exists():
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception:
                pass

    async def _index_semantic(self, query_text: str, key: str, prefix: str | None) -> None:
        try:
            await asyncio.to_thread(self.semantic.index, query_text, key, prefix)
        except Exception as e:
            logger.warning(f"Falha ao indexar cache semântico para {key!r}: {e}")

    # ── API pública ──────────────────────────────────────────────────────────

    async def get(
        self,
        prefix_or_key: str,
        query: str | None = None,
        check_etag: bool = False,
        etag_url: str | None = None,
    ) -> Any | None:
        if query is None:
            key = prefix_or_key
            filename = self._filename(prefix_or_key)
            prefix = None
        else:
            key = f"{prefix_or_key}:{query}"
            filename = self._filename(prefix_or_key, query)
            prefix = prefix_or_key

        self._access_counts[key] = self._access_counts.get(key, 0) + 1

        value = await self._read_tiers(key, filename)
        if value is not None:
            return value

        # Cache semântico: só se aplica ao modo "prefix + query" (texto livre).
        if self.semantic is not None and query is not None and self.semantic.enabled:
            match = self.semantic.find(query, prefix=prefix)
            if match is not None:
                matched_key, score = match
                meta = self._key_meta.get(matched_key)
                if meta is not None:
                    semantic_value = await self._read_tiers(matched_key, meta["filename"])
                    if semantic_value is not None:
                        logger.debug(
                            f"Cache HIT semântico ({score:.2%}): "
                            f"'{query[:60]}' ~ '{(meta.get('query') or matched_key)[:60]}'"
                        )
                        return semantic_value
                # Entrada indexada mas sem metadado correspondente (ex: expirou
                # e foi removida, ou processo reiniciado) — limpa e ignora.
                self.semantic.remove(matched_key)

        return None

    async def set(
        self,
        prefix_or_key: str,
        query_or_value: Any,
        value: Any = None,
        ttl_seconds: int | None = None,
        source_type: str = "default",
        etag: str | None = None,
        **kwargs: Any,
    ) -> None:
        if value is None:
            # SmartCache (key, value) — chave única, sem query em texto livre.
            key = prefix_or_key
            value_to_cache = query_or_value
            ttl_bucket = source_type
            filename = self._filename(prefix_or_key)
            query_text = None
            prefix = None
        else:
            # Cache antigo (prefix, query, value) — usado por SearchService.
            key = f"{prefix_or_key}:{query_or_value}"
            value_to_cache = value
            ttl_bucket = self._resolve_ttl_bucket(prefix_or_key, str(query_or_value))
            filename = self._filename(prefix_or_key, query_or_value)
            query_text = str(query_or_value)
            prefix = prefix_or_key

        ttl = ttl_seconds or kwargs.get("ttl") or self.TTL_STRATEGIES.get(ttl_bucket, self.TTL_STRATEGIES["default"])

        # Cache warming: queries que já bateram no threshold de popularidade
        # ganham TTL estendido (2x, até MAX_WARM_TTL) — reduz a chance de
        # expirar exatamente quando a demanda está alta.
        if self._access_counts.get(key, 0) >= self.warm_threshold:
            ttl = min(ttl * 2, self.MAX_WARM_TTL)

        await self._write_tiers(key, filename, value_to_cache, ttl)
        self._key_meta[key] = {
            "filename": filename,
            "prefix": prefix,
            "ttl": ttl,
            "query": query_text,
        }

        if self.semantic is not None and query_text and self.semantic.enabled:
            await self._index_semantic(query_text, key, prefix or ttl_bucket)

    async def delete(self, prefix_or_key: str, query: str | None = None) -> None:
        if query is None:
            key = prefix_or_key
            filename = self._filename(prefix_or_key)
        else:
            key = f"{prefix_or_key}:{query}"
            filename = self._filename(prefix_or_key, query)

        await self._delete_raw(key, filename)
        self._access_counts.pop(key, None)
        self._key_meta.pop(key, None)
        if self.semantic is not None:
            self.semantic.remove(key)

    async def invalidate(self, prefix: str) -> None:
        # Invalida memória + rastreamento de acesso/semântica
        keys_to_del = [k for k in self.memory if k.startswith(f"{prefix}:") or k == prefix]
        for k in keys_to_del:
            self.memory.pop(k, None)
            self._access_counts.pop(k, None)
            self._key_meta.pop(k, None)
            if self.semantic is not None:
                self.semantic.remove(k)

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
        def _unlink_glob():
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

    # ── Cache warming proativo ───────────────────────────────────────────────

    async def warm_popular(
        self,
        fetcher: Callable[[str], Awaitable[Any]],
        top_n: int = 10,
        min_hits: int | None = None,
    ) -> list[str]:
        """Reaquece proativamente as chaves mais acessadas, antes de expirarem.

        `fetcher(key)` deve retornar o valor fresco para aquela chave — o Cache
        não conhece a lógica de busca das fontes externas, então quem chama
        (ex: SearchService, um job do scheduler) decide como buscar dados novos.
        Chaves cujo fetch falhar ou retornar None são simplesmente ignoradas.

        Retorna a lista de chaves efetivamente reaquecidas.
        """
        threshold = min_hits if min_hits is not None else self.warm_threshold
        popular = sorted(
            (kv for kv in self._access_counts.items() if kv[1] >= threshold),
            key=lambda kv: kv[1],
            reverse=True,
        )[:top_n]

        warmed: list[str] = []
        for key, _hits in popular:
            meta = self._key_meta.get(key)
            if meta is None:
                continue
            try:
                fresh_value = await fetcher(key)
            except Exception as e:
                logger.warning(f"Cache warming falhou para {key!r}: {e}")
                continue
            if fresh_value is None:
                continue
            ttl = min(meta.get("ttl") or self.TTL_STRATEGIES["default"], self.MAX_WARM_TTL)
            await self._write_tiers(key, meta["filename"], fresh_value, ttl)
            warmed.append(key)
        return warmed

    def popular_queries(self, top_n: int = 10) -> list[tuple[str, int]]:
        """Lista as `top_n` chaves mais acessadas — usado para decidir o que reaquecer
        ou para observabilidade (ex: expor em /health ou métricas)."""
        return sorted(self._access_counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
