"""
Circuit breaker compartilhado.

Problema original: cada um dos 18 searchers reimplementava sua própria
lógica de "desistir de tentar depois de N falhas". Isso significava 18
implementações divergentes, sem visibilidade cruzada (o GithubSearcher não
sabia que o AwesomeSearcher também estava sofrendo timeout na mesma rede).

Solução: um único CircuitBreaker por *nome lógico de fonte*, obtido através
do CircuitBreakerRegistry (padrão registry/singleton). Todas as instâncias
de um mesmo searcher (e qualquer código que precise checar a saúde de uma
fonte) compartilham o mesmo estado.

Estados clássicos:
    CLOSED     -> tudo passa normalmente, falhas são contadas.
    OPEN       -> chamadas falham rápido (CircuitBreakerOpenError) sem tocar
                  a rede, até `recovery_timeout` se esgotar.
    HALF_OPEN  -> depois do timeout, deixa passar um número limitado de
                  chamadas de teste; se falharem, volta a OPEN; se
                  sucederem, fecha (CLOSED) novamente.
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .exceptions import CircuitBreakerOpenError

T = TypeVar("T")


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5  # falhas consecutivas até abrir
    recovery_timeout: float = 30.0  # segundos em OPEN antes de tentar HALF_OPEN
    half_open_max_calls: int = 1  # chamadas de teste permitidas em HALF_OPEN


@dataclass
class _BreakerState:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_calls_in_flight: int = 0


class CircuitBreaker:
    """Circuit breaker assíncrono, thread-safe via asyncio.Lock."""

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = _BreakerState()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state.state

    async def _time_since_open(self) -> float:
        return time.monotonic() - self._state.opened_at

    async def before_call(self) -> None:
        """Chamado antes de bater na fonte externa. Levanta se o circuito está aberto."""
        async with self._lock:
            if self._state.state is CircuitState.OPEN:
                elapsed = await self._time_since_open()
                remaining = self.config.recovery_timeout - elapsed
                if remaining > 0:
                    raise CircuitBreakerOpenError(self.name, remaining)
                # timeout de recuperação esgotado -> tenta HALF_OPEN
                self._state.state = CircuitState.HALF_OPEN
                self._state.half_open_calls_in_flight = 0

            if self._state.state is CircuitState.HALF_OPEN:
                if (
                    self._state.half_open_calls_in_flight
                    >= self.config.half_open_max_calls
                ):
                    raise CircuitBreakerOpenError(
                        self.name, self.config.recovery_timeout
                    )
                self._state.half_open_calls_in_flight += 1

    async def on_success(self) -> None:
        async with self._lock:
            self._state.consecutive_failures = 0
            self._state.state = CircuitState.CLOSED
            self._state.half_open_calls_in_flight = 0

    async def on_failure(self) -> None:
        async with self._lock:
            self._state.consecutive_failures += 1
            if self._state.state is CircuitState.HALF_OPEN:
                # falhou o teste de recuperação -> volta a abrir imediatamente
                self._state.state = CircuitState.OPEN
                self._state.opened_at = time.monotonic()
                self._state.half_open_calls_in_flight = 0
                return
            if self._state.consecutive_failures >= self.config.failure_threshold:
                self._state.state = CircuitState.OPEN
                self._state.opened_at = time.monotonic()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Executa `fn` protegida pelo circuit breaker."""
        await self.before_call()
        try:
            result = await fn()
        except Exception:
            await self.on_failure()
            raise
        else:
            await self.on_success()
            return result


class CircuitBreakerRegistry:
    """Registry global: garante um único CircuitBreaker por nome de fonte."""

    _breakers: dict[str, CircuitBreaker] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def get(
        cls, name: str, config: CircuitBreakerConfig | None = None
    ) -> CircuitBreaker:
        async with cls._lock:
            if name not in cls._breakers:
                cls._breakers[name] = CircuitBreaker(name, config)
            return cls._breakers[name]

    @classmethod
    def reset_all(cls) -> None:
        """Útil em testes."""
        cls._breakers.clear()
