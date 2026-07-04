"""
Rate limiter compartilhado (token bucket assíncrono).

Problema original: cada searcher tinha seu próprio contador de "quantas
chamadas por minuto", geralmente um `time.sleep` improvisado ou um contador
sem lock, incorreto sob concorrência (vários workers chamando o mesmo
searcher ao mesmo tempo).

Solução: um único RateLimiter por fonte, obtido via RateLimiterRegistry,
implementando token bucket com refill contínuo baseado em `capacity` e
`refill_rate` (tokens/segundo). Suporta:
  - `acquire()`: espera até haver um token disponível (uso normal).
  - `try_acquire()`: falha rápido com RateLimitExceededError se não há
    token disponível (útil quando o chamador prefere backoff próprio).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .exceptions import RateLimitExceededError


@dataclass
class RateLimiterConfig:
    capacity: int = 10          # tamanho máximo do bucket (burst)
    refill_rate: float = 1.0    # tokens adicionados por segundo


class RateLimiter:
    def __init__(self, name: str, config: RateLimiterConfig | None = None):
        self.name = name
        self.config = config or RateLimiterConfig()
        self._tokens = float(self.config.capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self.config.capacity, self._tokens + elapsed * self.config.refill_rate
        )
        self._last_refill = now

    async def try_acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            self._refill()
            if self._tokens < cost:
                wait_seconds = (cost - self._tokens) / self.config.refill_rate
                raise RateLimitExceededError(self.name, wait_seconds)
            self._tokens -= cost

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait_seconds = (cost - self._tokens) / self.config.refill_rate
            await asyncio.sleep(wait_seconds)


class RateLimiterRegistry:
    """Registry global: garante um único RateLimiter por nome de fonte."""

    _limiters: dict[str, RateLimiter] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def get(
        cls, name: str, config: RateLimiterConfig | None = None
    ) -> RateLimiter:
        async with cls._lock:
            if name not in cls._limiters:
                cls._limiters[name] = RateLimiter(name, config)
            return cls._limiters[name]

    @classmethod
    def reset_all(cls) -> None:
        """Útil em testes."""
        cls._limiters.clear()
