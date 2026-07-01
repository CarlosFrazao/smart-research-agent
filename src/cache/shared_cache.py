"""
shared_cache.py — Cache Compartilhado Redis com Fallback em Memória

Permite que múltiplos agentes do ecossistema Antigravity compartilhem
resultados de scraping e pesquisa sem duplicar chamadas de rede.

Estratégias de TTL:
  - aggressive: 7 dias (radar, guerrilha)
  - moderate:   48 horas (concorrencia)
  - minimal:    1 hora (cirurgia, black_ops)
  - permanent:  30 dias (arqueologia)

Fallback: se Redis não estiver disponível, opera com dict em memória.

Skill: systematic-debugging (Cache distribuído)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── TTL em segundos por estratégia ───────────────────────────────────────────
TTL_STRATEGIES: Dict[str, int] = {
    "aggressive": 7 * 24 * 3600,    # 7 dias
    "moderate":   48 * 3600,         # 48 horas
    "minimal":    3600,               # 1 hora
    "permanent":  30 * 24 * 3600,   # 30 dias
}
DEFAULT_TTL = TTL_STRATEGIES["moderate"]


class _InMemoryCache:
    """Fallback de dict em memória com TTL manual — sem dependências externas."""

    def __init__(self) -> None:
        self._store: Dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.time() + ttl)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def scan_keys(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    def clear(self) -> None:
        self._store.clear()

    @property
    def backend_name(self) -> str:
        return "in_memory"


class SharedCache:
    """
    Cache compartilhado com backend Redis (primário) ou dict em memória (fallback).

    Uso:
        cache = SharedCache()
        cache.set_scraped_content("https://example.com", "# Markdown content")
        content = cache.get_scraped_content("https://example.com")

    Com estratégia de TTL:
        cache.set_research_result("my-query-hash", result, strategy="aggressive")
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        default_strategy: str = "moderate",
    ) -> None:
        self._default_strategy = default_strategy
        self._backend: Any = None
        self._is_redis = False

        try:
            import redis
            client = redis.from_url(redis_url, socket_connect_timeout=2)
            client.ping()
            self._backend = client
            self._is_redis = True
            logger.info(f"SharedCache: Redis conectado em {redis_url}")
        except Exception as e:
            logger.warning(
                f"SharedCache: Redis indisponível ({e}). "
                "Usando cache em memória como fallback."
            )
            self._backend = _InMemoryCache()

    @property
    def backend_name(self) -> str:
        return "redis" if self._is_redis else "in_memory"

    # ── API Genérica ─────────────────────────────────────────────────────────

    def _ttl(self, strategy: Optional[str] = None, ttl: Optional[int] = None) -> int:
        if ttl is not None:
            return ttl
        s = strategy or self._default_strategy
        return TTL_STRATEGIES.get(s, DEFAULT_TTL)

    def get(self, key: str) -> Optional[Any]:
        try:
            if self._is_redis:
                raw = self._backend.get(key)
                return json.loads(raw) if raw else None
            return self._backend.get(key)
        except Exception as e:
            logger.warning(f"SharedCache.get({key!r}): {e}")
            return None

    def set(
        self,
        key: str,
        value: Any,
        strategy: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> None:
        effective_ttl = self._ttl(strategy, ttl)
        try:
            if self._is_redis:
                self._backend.setex(key, effective_ttl, json.dumps(value, default=str))
            else:
                self._backend.set(key, value, effective_ttl)
        except Exception as e:
            logger.warning(f"SharedCache.set({key!r}): {e}")

    def delete(self, key: str) -> None:
        try:
            if self._is_redis:
                self._backend.delete(key)
            else:
                self._backend.delete(key)
        except Exception as e:
            logger.warning(f"SharedCache.delete({key!r}): {e}")

    def invalidate_by_prefix(self, prefix: str) -> int:
        """Remove todas as chaves que começam com `prefix`. Retorna count."""
        count = 0
        try:
            if self._is_redis:
                for key in self._backend.scan_iter(match=f"{prefix}*"):
                    self._backend.delete(key)
                    count += 1
            else:
                for key in self._backend.scan_keys(prefix):
                    self._backend.delete(key)
                    count += 1
        except Exception as e:
            logger.warning(f"SharedCache.invalidate_by_prefix({prefix!r}): {e}")
        return count

    # ── API Semântica — Scraping ─────────────────────────────────────────────

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def get_scraped_content(self, url: str) -> Optional[str]:
        key = f"scrape:{self.url_hash(url)}"
        result = self.get(key)
        if result is not None:
            logger.debug(f"SharedCache SCRAPE HIT: {url[:60]}")
        return result

    def set_scraped_content(
        self,
        url: str,
        content: str,
        strategy: Optional[str] = None,
    ) -> None:
        key = f"scrape:{self.url_hash(url)}"
        self.set(key, content, strategy=strategy)
        logger.debug(f"SharedCache SCRAPE SET: {url[:60]} [{strategy or self._default_strategy}]")

    # ── API Semântica — Research Results ────────────────────────────────────

    @staticmethod
    def query_hash(query: str) -> str:
        return hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]

    def get_research_result(self, query: str) -> Optional[Dict]:
        key = f"research:{self.query_hash(query)}"
        result = self.get(key)
        if result is not None:
            logger.debug(f"SharedCache RESEARCH HIT: {query[:60]}")
        return result

    def set_research_result(
        self,
        query: str,
        result: Dict,
        strategy: Optional[str] = None,
    ) -> None:
        key = f"research:{self.query_hash(query)}"
        self.set(key, result, strategy=strategy)
        logger.debug(f"SharedCache RESEARCH SET: {query[:60]}")

    # ── Utilitários ──────────────────────────────────────────────────────────

    def clear_all(self) -> None:
        """Limpa todo o cache (usar apenas em testes)."""
        try:
            if self._is_redis:
                self._backend.flushdb()
            else:
                self._backend.clear()
        except Exception as e:
            logger.warning(f"SharedCache.clear_all(): {e}")
