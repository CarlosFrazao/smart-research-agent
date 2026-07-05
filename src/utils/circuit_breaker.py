"""Circuit Breaker — Desliga automaticamente fontes que falham repetidamente.

Implementa o padrão Circuit Breaker v7.0:
  - Estados: CLOSED (normal) → OPEN (desligado) → HALF_OPEN (testando recuperação)
  - Configuração fina com CircuitBreakerConfig por breaker/source.
  - Backoff exponencial com jitter para o recovery_timeout de abertura.
  - Registro de métricas avançadas (latência, transições, falhas).
  - Decorator @with_circuit_breaker para aplicação não-invasiva.
  - Registry assíncrono e thread-safe.
  - Compatibilidade 100% retroativa com assinaturas síncronas/métricas legadas do HealthMonitor.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("utils.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"  # Operação normal
    OPEN = "open"  # Desligado — rejeita chamadas rápido
    HALF_OPEN = "half_open"  # Testando se o serviço se recuperou


class CircuitBreakerOpen(Exception):
    """Circuit breaker está aberto — serviço temporariamente indisponível."""

    def __init__(
        self,
        message: str = "Circuit Breaker OPEN",
        name: str | None = None,
        remaining: float | None = None,
        state: Any = None,
    ):
        super().__init__(message)
        self.name = name
        self.remaining = remaining
        self.state = state


@dataclass
class CircuitBreakerConfig:
    """Configuração operacional de um Circuit Breaker."""

    name: str
    failure_threshold: int = 3  # Falhas consecutivas para abrir
    recovery_timeout: float = 60.0  # Timeout inicial de recuperação (segundos)
    recovery_timeout_max: float = 300.0  # Timeout de recuperação máximo com backoff
    half_open_max_calls: int = 1  # Quantidade de testes permitidos em HALF_OPEN
    backoff_factor: float = 2.0  # Fator multiplicador do backoff exponencial
    jitter: bool = (
        True  # Adiciona ruído aleatório ao timeout para evitar thundering herd
    )


@dataclass
class CircuitMetrics:
    """Métricas operacionais e telemetria do Circuit Breaker."""

    total_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    total_rejects: int = 0
    consecutive_failures: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    opened_at: Optional[float] = None
    last_error: Optional[str] = None
    state_transitions: List[Dict[str, Any]] = field(default_factory=list)


class CircuitBreaker:
    """Circuit Breaker assíncrono, thread-safe, com backoff exponencial e jitter.

    Compatível com chamadas via `call()`, via decorator `@with_circuit_breaker` ou
    gravação manual/síncrona de sucessos e falhas (`record_success`/`record_failure`).
    """

    def __init__(self, config: CircuitBreakerConfig | str, **kwargs):
        """
        Aceita tanto uma instância de `CircuitBreakerConfig` quanto um nome de string
        com argumentos via kwargs (para retrocompatibilidade).
        """
        if isinstance(config, str):
            self.cfg = CircuitBreakerConfig(
                name=config,
                failure_threshold=kwargs.get("failure_threshold", 3),
                recovery_timeout=kwargs.get("recovery_timeout", 300.0),
                recovery_timeout_max=kwargs.get("recovery_timeout_max", 300.0),
                half_open_max_calls=kwargs.get("half_open_max_calls", 1),
            )
        else:
            self.cfg = config

        self.name = self.cfg.name
        self._state = CircuitState.CLOSED
        self.metrics_data = CircuitMetrics()
        self._consecutive_opens = 0  # Usado no cálculo do backoff exponencial
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @state.setter
    def state(self, new_state: CircuitState) -> None:
        """Setter para retrocompatibilidade com código que modifica o estado diretamente."""
        self._state = new_state

    @property
    def failure_count(self) -> int:
        return self.metrics_data.consecutive_failures

    @failure_count.setter
    def failure_count(self, value: int) -> None:
        self.metrics_data.consecutive_failures = value

    @property
    def last_failure_time(self) -> Optional[float]:
        return self.metrics_data.last_failure_time

    @last_failure_time.setter
    def last_failure_time(self, value: Optional[float]) -> None:
        self.metrics_data.last_failure_time = value

    @property
    def opened_at(self) -> Optional[float]:
        return self.metrics_data.opened_at

    @opened_at.setter
    def opened_at(self, value: Optional[float]) -> None:
        self.metrics_data.opened_at = value

    @property
    def total_failures(self) -> int:
        return self.metrics_data.total_failures

    @total_failures.setter
    def total_failures(self, value: int) -> None:
        self.metrics_data.total_failures = value

    @property
    def total_successes(self) -> int:
        return self.metrics_data.total_successes

    @total_successes.setter
    def total_successes(self, value: int) -> None:
        self.metrics_data.total_successes = value

    @property
    def last_error(self) -> Optional[str]:
        return self.metrics_data.last_error

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self.metrics_data.last_error = value

    @property
    def failure_threshold(self) -> int:
        return self.cfg.failure_threshold

    @property
    def recovery_timeout(self) -> float:
        """Calcula o recovery timeout dinamicamente com backoff exponencial e jitter."""
        if self._consecutive_opens == 0:
            return self.cfg.recovery_timeout

        # Backoff exponencial: base * (factor ^ consecutive_opens)
        timeout = self.cfg.recovery_timeout * (
            self.cfg.backoff_factor ** (self._consecutive_opens - 1)
        )
        timeout = min(timeout, self.cfg.recovery_timeout_max)

        # Adiciona Jitter (ruído aleatório de +/- 10% do valor)
        if self.cfg.jitter:
            jitter_range = timeout * 0.10
            timeout += random.uniform(-jitter_range, jitter_range)

        return round(timeout, 3)

    # ── Execução Assíncrona Protegida ────────────────────────────────────────

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Executa a função corrotina func protegida pelo circuit breaker."""
        async with self._lock:
            self._check_and_update_state()

            if self._state == CircuitState.OPEN:
                self.metrics_data.total_rejects += 1
                remaining = self._get_remaining_recovery_time()
                raise CircuitBreakerOpen(
                    f"Circuit '{self.name}' OPEN. Rejeitando chamada. Retry em {remaining:.1f}s"
                )

            if self._state == CircuitState.HALF_OPEN:
                if self.metrics_data.total_calls >= self.cfg.half_open_max_calls:
                    self.metrics_data.total_rejects += 1
                    raise CircuitBreakerOpen(
                        f"Circuit '{self.name}' HALF_OPEN: limite de chamadas de teste atingido."
                    )

            self.metrics_data.total_calls += 1

        start_time = time.monotonic()
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            await self._on_failure(str(exc))
            raise

    # ── API Síncrona/Direct Reporting (HealthMonitor & Searchers legados) ──────

    def allow_request(self) -> bool:
        """Retorna True se o circuito aceitar requisições de teste."""
        self._check_and_update_state()

        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            return False

        # HALF_OPEN
        if self.metrics_data.total_calls >= self.cfg.half_open_max_calls:
            return False

        self.metrics_data.total_calls += 1
        return True

    def record_success(self) -> None:
        """Registra um sucesso reportado de forma síncrona/externa."""
        self.metrics_data.total_successes += 1
        self.metrics_data.consecutive_failures = 0
        self.metrics_data.last_success_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.CLOSED, "Direct success in HALF_OPEN")
            self._consecutive_opens = 0
            self.metrics_data.opened_at = None

    def record_failure(self, error: str | None = None) -> None:
        """Registra uma falha reportada de forma síncrona/externa."""
        self.metrics_data.total_failures += 1
        self.metrics_data.consecutive_failures += 1
        self.metrics_data.last_failure_time = time.time()
        self.metrics_data.last_error = error or "Reported failure"

        if self._state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN, f"Failure in HALF_OPEN: {error}")
        elif (
            self._state == CircuitState.CLOSED
            and self.metrics_data.consecutive_failures >= self.cfg.failure_threshold
        ):
            self._transition_to(
                CircuitState.OPEN, f"Failure threshold reached: {error}"
            )

    def reset(self) -> None:
        """Reseta completamente o estado e as métricas do breaker."""
        self._state = CircuitState.CLOSED
        self._consecutive_opens = 0
        self.metrics_data = CircuitMetrics()

    # ── Internos ─────────────────────────────────────────────────────────────

    async def _on_success(self) -> None:
        async with self._lock:
            self.record_success()

    async def _on_failure(self, error: str | None = None) -> None:
        async with self._lock:
            self.record_failure(error or "Unspecified error")

    def _check_and_update_state(self) -> None:
        """Atualiza o estado baseado em timeouts expirados."""
        if (
            self._state == CircuitState.OPEN
            and self.metrics_data.last_failure_time is not None
        ):
            elapsed = time.time() - self.metrics_data.last_failure_time
            timeout = self.recovery_timeout
            if elapsed >= timeout:
                self._transition_to(
                    CircuitState.HALF_OPEN,
                    f"Recovery timeout expired ({elapsed:.1f}s >= {timeout:.1f}s)",
                )

    def _transition_to(self, new_state: CircuitState, reason: str) -> None:
        if self._state == new_state:
            return
        old_state = self._state
        self._state = new_state
        timestamp = time.time()

        if new_state == CircuitState.OPEN:
            self._consecutive_opens += 1
            self.metrics_data.opened_at = timestamp
        elif new_state == CircuitState.HALF_OPEN:
            self.metrics_data.total_calls = 0
            self.metrics_data.total_rejects = 0

        self.metrics_data.state_transitions.append(
            {
                "from": old_state.value,
                "to": new_state.value,
                "timestamp": timestamp,
                "reason": reason,
            }
        )

        logger.info(
            f"CircuitBreaker [{self.name}]: {old_state.value} → {new_state.value} "
            f"(reason: {reason}, consecutive_opens: {self._consecutive_opens})"
        )

    def _get_remaining_recovery_time(self) -> float:
        if self.metrics_data.last_failure_time is None:
            return 0.0
        elapsed = time.time() - self.metrics_data.last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)

    # ── Métricas e Status de Compatibilidade ─────────────────────────────────

    @property
    def status(self) -> dict:
        """Dicionário de status para consumo do HealthMonitor legado."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self.metrics_data.consecutive_failures,
            "last_failure": self.metrics_data.last_failure_time,
        }

    @property
    def metrics(self) -> dict:
        """Métricas detalhadas para exibição e HealthMonitor."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self.metrics_data.consecutive_failures,
            "failure_threshold": self.cfg.failure_threshold,
            "total_failures": self.metrics_data.total_failures,
            "total_successes": self.metrics_data.total_successes,
            "total_calls": self.metrics_data.total_calls,
            "total_rejects": self.metrics_data.total_rejects,
            "last_error": self.metrics_data.last_error,
            "last_failure_time": self.metrics_data.last_failure_time,
            "opened_at": self.metrics_data.opened_at,
            "recovery_timeout": self.recovery_timeout,
            "consecutive_opens": self._consecutive_opens,
            "transitions_count": len(self.metrics_data.state_transitions),
        }

    def get_metrics(self) -> dict:
        """Alias para metrics."""
        return self.metrics

    def get_status(self) -> dict:
        """Alias para status."""
        return self.status


# ── Registry Centralizado Async-Safe ────────────────────────────────────────


class CircuitBreakerRegistry:
    """Registro thread-safe/async-safe de circuit breakers por source."""

    _breakers: dict[str, CircuitBreaker] = {}
    _lock = asyncio.Lock()

    # Defaults de classe que podem ser atualizados na instanciação
    default_failure_threshold: int = 3
    default_recovery_timeout: float = 300.0

    def __init__(
        self, default_failure_threshold: int = 5, default_recovery_timeout: float = 60.0
    ):
        # Atualiza os defaults de classe na instanciação
        CircuitBreakerRegistry.default_failure_threshold = default_failure_threshold
        CircuitBreakerRegistry.default_recovery_timeout = default_recovery_timeout

    @classmethod
    async def get_or_create(
        cls, source_name: str, config: CircuitBreakerConfig | None = None, **kwargs
    ) -> CircuitBreaker:
        """Obtém ou cria um breaker de forma assíncrona (compatibilidade com APISearcher)."""
        return await cls.get_async(source_name, config=config, **kwargs)

    @classmethod
    async def get_async(
        cls, source_name: str, config: CircuitBreakerConfig | None = None, **kwargs
    ) -> CircuitBreaker:
        """Obtém ou cria um breaker de forma assíncrona protegida por Lock."""
        async with cls._lock:
            if source_name not in cls._breakers:
                if config is None:
                    config = CircuitBreakerConfig(name=source_name, **kwargs)
                cls._breakers[source_name] = CircuitBreaker(config)
            return cls._breakers[source_name]

    @classmethod
    def get(cls, source_name: str, **kwargs) -> CircuitBreaker:
        """Obtém ou cria um breaker de forma síncrona."""
        if source_name not in cls._breakers:
            failure_threshold = kwargs.get("failure_threshold")
            if failure_threshold is None:
                failure_threshold = cls.default_failure_threshold

            recovery_timeout = kwargs.get("recovery_timeout")
            if recovery_timeout is None:
                recovery_timeout = cls.default_recovery_timeout

            config = CircuitBreakerConfig(
                name=source_name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
            cls._breakers[source_name] = CircuitBreaker(config)
        return cls._breakers[source_name]

    @classmethod
    def remove(cls, source_name: str) -> None:
        cls._breakers.pop(source_name, None)

    @classmethod
    def status_all(cls) -> dict[str, str]:
        return {name: b.state.value for name, b in cls._breakers.items()}

    @classmethod
    def metrics_all(cls) -> dict[str, dict]:
        return {name: b.metrics for name, b in cls._breakers.items()}

    def all_metrics(self) -> dict[str, dict]:
        """Retorna métricas de todos os breakers no registry (uso em instâncias)."""
        return {name: b.metrics for name, b in self._breakers.items()}

    @classmethod
    def reset_all(cls) -> None:
        for b in cls._breakers.values():
            b.reset()
        cls._breakers.clear()

    @classmethod
    def open_circuits(cls) -> list[str]:
        """Retorna nomes de fontes cujos circuitos estão OPEN."""
        return [
            name for name, b in cls._breakers.items() if b.state == CircuitState.OPEN
        ]


# Registry padrão global
_DEFAULT_REGISTRY = CircuitBreakerRegistry


async def get_default_registry() -> type[CircuitBreakerRegistry]:
    """Retorna o registry global de circuit breakers."""
    return _DEFAULT_REGISTRY


# ── Decorator de Execução Protegida ─────────────────────────────────────────


def with_circuit_breaker(
    name: str, config: CircuitBreakerConfig | None = None, **cb_kwargs
):
    """Decorator assíncrono para envolver métodos de busca com o Circuit Breaker.

    Uso:
        class GitHubSearcher(BaseSearcher):
            @with_circuit_breaker("github", failure_threshold=3)
            async def search(self, query: str) -> list[SearchResult]:
                ...
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Obtém o breaker de forma assíncrona e executa a chamada
            breaker = CircuitBreakerRegistry.get(name, **cb_kwargs)
            return await breaker.call(func, *args, **kwargs)

        return wrapper

    return decorator
