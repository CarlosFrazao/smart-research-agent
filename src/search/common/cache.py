"""
Cache compartilhado, com TTL, por fonte.

Problema original: cada searcher tinha seu próprio dict/lru_cache ad-hoc
para não bater na API externa repetidamente com a mesma query, sem TTL
consistente, sem namespace (risco de colisão de chave entre searchers) e
sem forma de trocar por um backend real (Redis) sem editar 18 arquivos.

Solução: `CacheBackend` é uma interface mínima (get/set/delete). O padrão
é `InMemoryTTLCache`, mas em produção basta registrar um backend Redis (ou
outro) uma única vez via `CacheRegistry.set_backend_factory(...)` e todos os
searchers passam a usá-lo sem qualquer alteração de código.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Callable


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, key: str) -> Any | None: ...

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: float) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class InMemoryTTLCache(CacheBackend):
    """Backend padrão: dict em memória do processo, com expiração por TTL."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic() + ttl_seconds, value)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


class NamespacedCache:
    """Wrapper fino que prefixa chaves com o nome da fonte (evita colisão)."""

    def __init__(self, namespace: str, backend: CacheBackend, default_ttl: float):
        self.namespace = namespace
        self._backend = backend
        self.default_ttl = default_ttl

    def _key(self, raw_key: str) -> str:
        return f"search:{self.namespace}:{raw_key}"

    async def get(self, raw_key: str) -> Any | None:
        return await self._backend.get(self._key(raw_key))

    async def set(
        self, raw_key: str, value: Any, ttl_seconds: float | None = None
    ) -> None:
        await self._backend.set(
            self._key(raw_key), value, ttl_seconds or self.default_ttl
        )

    async def delete(self, raw_key: str) -> None:
        await self._backend.delete(self._key(raw_key))


class CacheRegistry:
    """
    Registry global de caches por namespace (nome da fonte).

    `set_backend_factory` permite trocar o backend padrão (ex: para Redis)
    em um único lugar, no bootstrap da aplicação, sem tocar nos searchers.
    """

    _caches: dict[str, NamespacedCache] = {}
    _backend_factory: Callable[[], CacheBackend] = InMemoryTTLCache
    _lock = asyncio.Lock()

    @classmethod
    def set_backend_factory(cls, factory: Callable[[], CacheBackend]) -> None:
        cls._backend_factory = factory
        cls._caches.clear()  # força recriação com o novo backend

    @classmethod
    async def get(cls, namespace: str, default_ttl: float = 300.0) -> NamespacedCache:
        async with cls._lock:
            if namespace not in cls._caches:
                backend = cls._backend_factory()
                cls._caches[namespace] = NamespacedCache(
                    namespace, backend, default_ttl
                )
            return cls._caches[namespace]

    @classmethod
    def reset_all(cls) -> None:
        """Útil em testes."""
        cls._caches.clear()
