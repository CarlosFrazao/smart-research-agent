"""
Circuit Breaker — Desliga automaticamente fontes que falham repetidamente.
Estados: CLOSED (normal) → OPEN (desligado) → HALF_OPEN (testando recuperação)
"""
import asyncio
import logging
import time
from enum import Enum
from collections.abc import Callable

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"        # Operação normal
    OPEN = "open"            # Desligado — não chamar
    HALF_OPEN = "half_open"  # Testando se voltou


class CircuitBreakerOpen(Exception):
    """Circuit breaker está aberto — serviço temporariamente indisponível."""
    pass


class CircuitBreaker:
    """
    Circuit Breaker para serviços externos.

    Args:
        name: Nome do serviço (ex: \"github_api\")
        failure_threshold: Falhas consecutivas para abrir (default: 3)
        recovery_timeout: Segundos para tentar novamente (default: 300 = 5min)
        half_open_max_calls: Chamadas de teste no half_open (default: 1)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 300.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs):
        """Executa função com proteção do circuit breaker."""
        async with self._lock:
            if self.state == CircuitState.OPEN:
                elapsed = time.time() - (self.last_failure_time or 0)
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    logger.info(f"Circuit {self.name}: OPEN → HALF_OPEN (testando)")
                else:
                    remaining = self.recovery_timeout - elapsed
                    raise CircuitBreakerOpen(
                        f"Circuit '{self.name}' OPEN. Retry em {remaining:.0f}s"
                    )

            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpen(f"Circuit '{self.name}': limite half_open atingido")
                self.half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception:
            await self._on_failure()
            raise

    async def _on_success(self):
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                logger.info(f"Circuit {self.name}: HALF_OPEN → CLOSED ✅")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    async def _on_failure(self):
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit {self.name}: HALF_OPEN → OPEN (falha no teste)")
            elif self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit {self.name}: CLOSED → OPEN após {self.failure_count} falhas")

    def reset(self) -> None:
        """Reset completo do circuit breaker (uso em testes ou admin)."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.half_open_calls = 0

    @property
    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure": self.last_failure_time,
        }


# Registry global de circuit breakers
class CircuitBreakerRegistry:
    _breakers: dict[str, CircuitBreaker] = {}

    @classmethod
    def get(cls, source_name: str, **kwargs) -> CircuitBreaker:
        if source_name not in cls._breakers:
            cls._breakers[source_name] = CircuitBreaker(source_name, **kwargs)
        return cls._breakers[source_name]

    @classmethod
    def status_all(cls) -> dict[str, str]:
        return {name: b.state.value for name, b in cls._breakers.items()}

    @classmethod
    def reset_all(cls) -> None:
        """Reset de todos os breakers (uso em testes)."""
        for b in cls._breakers.values():
            b.reset()
        cls._breakers.clear()
