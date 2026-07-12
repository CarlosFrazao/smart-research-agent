"""Resilient Retry Utility with Exponential Backoff, Jitter, and Circuit Breaker Integration.

Implements the SRA v7.0 resilience patterns:
  - Supports both async and sync functions.
  - Custom RetryConfig & RetryResult.
  - Backoff formula: min_wait * backoff_factor^(attempt-1) + Jitter.
  - Fast-fail aborting when the associated Circuit Breaker is OPEN.
  - Logging and retry hooks.
  - 100% backward compatible with tenacity-style build_async_retrying and legacy config names.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Type, Sequence, Optional
import tenacity
from tenacity import (
    before_sleep_log,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger("utils.retry")

# Para evitar import circular, referenciamos o CircuitBreaker por duck typing ou type annotation
try:
    from src.utils.circuit_breaker import CircuitBreaker, CircuitState
except ImportError:
    CircuitBreaker = Any
    CircuitState = Any


@dataclass
class RetryConfig:
    """Configuração para política de retentativas.

    Mapeia tanto a nova nomenclatura v7.0 quanto a legada v6.0/tenacity.
    """

    # Atributos v7.0
    max_attempts: Optional[int] = None
    min_wait: Optional[float] = None
    max_wait: Optional[float] = None
    backoff_factor: Optional[float] = None
    jitter_ratio: float = 0.25
    expected_exceptions: Optional[Sequence[Type[Exception]]] = None
    circuit_breaker: Optional[Any] = None  # Instância de CircuitBreaker
    on_retry: Optional[Callable[[int, Exception, float], None]] = (
        None  # Hook callback(attempt, exception, wait_seconds)
    )

    # Atributos legados/v6.0/tenacity
    max_retries: Optional[int] = None
    base_delay: Optional[float] = None
    max_delay: Optional[float] = None
    exponential_base: Optional[float] = None
    jitter: bool = True
    retryable_exceptions: Optional[Sequence[Type[Exception]]] = None
    retry_on: Optional[Sequence[Type[Exception]]] = None
    initial_wait_seconds: Optional[float] = None
    max_wait_seconds: Optional[float] = None

    def __post_init__(self):
        # Unifica max_attempts / max_retries
        if self.max_attempts is None:
            if self.max_retries is not None:
                self.max_attempts = self.max_retries + 1
            else:
                self.max_attempts = 3
        if self.max_retries is None:
            self.max_retries = max(0, self.max_attempts - 1)

        # Unifica min_wait / base_delay / initial_wait_seconds
        if self.min_wait is None:
            if self.initial_wait_seconds is not None:
                self.min_wait = self.initial_wait_seconds
            elif self.base_delay is not None:
                self.min_wait = self.base_delay
            else:
                self.min_wait = 1.0
        if self.base_delay is None:
            self.base_delay = self.min_wait
        if self.initial_wait_seconds is None:
            self.initial_wait_seconds = self.min_wait

        # Unifica max_wait / max_delay / max_wait_seconds
        if self.max_wait is None:
            if self.max_wait_seconds is not None:
                self.max_wait = self.max_wait_seconds
            elif self.max_delay is not None:
                self.max_wait = self.max_delay
            else:
                self.max_wait = 60.0
        if self.max_delay is None:
            self.max_delay = self.max_wait
        if self.max_wait_seconds is None:
            self.max_wait_seconds = self.max_wait

        # Unifica backoff_factor / exponential_base
        if self.backoff_factor is None:
            if self.exponential_base is not None:
                self.backoff_factor = self.exponential_base
            else:
                self.backoff_factor = 2.0
        if self.exponential_base is None:
            self.exponential_base = self.backoff_factor

        # Unifica expected_exceptions / retry_on / retryable_exceptions
        if self.expected_exceptions is None:
            if self.retry_on is not None:
                self.expected_exceptions = self.retry_on
            elif self.retryable_exceptions is not None:
                self.expected_exceptions = self.retryable_exceptions
            else:
                self.expected_exceptions = (Exception,)
        if self.retry_on is None:
            self.retry_on = self.expected_exceptions
        if self.retryable_exceptions is None:
            self.retryable_exceptions = self.expected_exceptions

        if not self.jitter:
            self.jitter_ratio = 0.0


@dataclass
class RetryResult:
    """Resultado final de uma execução protegida por retry."""

    value: Any
    attempts: int
    total_wait_seconds: float
    last_exception: Optional[Exception] = None

    @property
    def success(self) -> bool:
        return self.last_exception is None


def calculate_backoff(attempt: int, config: RetryConfig) -> float:
    """Calcula o tempo de espera para a tentativa usando backoff exponencial e jitter."""
    if attempt <= 1:
        return 0.0

    # __post_init__ garante que estes campos não são None; normalizamos para
    # float local para que o type-checker acompanhe (e como rede de segurança).
    min_wait = config.min_wait if config.min_wait is not None else 1.0
    backoff_factor = config.backoff_factor if config.backoff_factor is not None else 2.0
    max_wait = config.max_wait if config.max_wait is not None else 60.0

    # Exponencial: min_wait * backoff_factor^(attempt-2)
    wait = min_wait * (backoff_factor ** (attempt - 2))
    wait = min(wait, max_wait)

    # Aplica Jitter
    if config.jitter_ratio > 0.0:
        jitter_range = wait * config.jitter_ratio
        wait += random.uniform(-jitter_range, jitter_range)
        wait = max(0.0, wait)

    return round(wait, 3)


def log_retry_summary(name: str, result: RetryResult) -> None:
    """Loga o resumo das tentativas."""
    if result.success:
        logger.info(
            f"Retry execution [{name}] succeeded after {result.attempts} attempt(s) "
            f"(Total wait: {result.total_wait_seconds:.2f}s)"
        )
    else:
        logger.error(
            f"Retry execution [{name}] failed after {result.attempts} attempt(s) "
            f"(Total wait: {result.total_wait_seconds:.2f}s). Last error: {result.last_exception}"
        )


class CircuitBreakerStop:
    """Critério de parada personalizado para Tenacity que falha se o Circuit Breaker estiver OPEN."""

    def __init__(self, circuit_breaker: Optional[Any], stop_policy: Any):
        self.circuit_breaker = circuit_breaker
        self.stop_policy = stop_policy

    def __call__(self, retry_state: Any) -> bool:
        if self.circuit_breaker is not None:
            cb_state = getattr(self.circuit_breaker, "state", None)
            if cb_state == "open" or (
                hasattr(cb_state, "value") and cb_state.value == "open"
            ):
                from src.utils.circuit_breaker import CircuitBreakerOpen

                raise CircuitBreakerOpen(
                    f"Circuit Breaker '{getattr(self.circuit_breaker, 'name', 'unnamed')}' is OPEN. Abortando retries."
                )
        return self.stop_policy(retry_state)


def build_async_retrying(config: RetryConfig) -> tenacity.AsyncRetrying:
    """Retorna um objeto AsyncRetrying do Tenacity configurado a partir de RetryConfig.

    Permite suporte ao loop tenacity nativo:
        async for attempt in retrying:
            with attempt:
                ...
    """
    stop_policy = stop_after_attempt(config.max_attempts)
    wrapped_stop = CircuitBreakerStop(config.circuit_breaker, stop_policy)

    exceptions = tuple(config.expected_exceptions)

    # Callback antes de dormir para registrar sucesso/falha do breaker se presente
    def before_sleep_cb(retry_state):
        if config.circuit_breaker is not None and hasattr(
            config.circuit_breaker, "record_failure"
        ):
            config.circuit_breaker.record_failure(str(retry_state.outcome.exception()))

        # Chama callback de retry do usuário
        if config.on_retry is not None:
            try:
                attempt = retry_state.attempt_number
                exc = retry_state.outcome.exception()
                wait = retry_state.next_action.sleep
                config.on_retry(attempt, exc, wait)
            except Exception as hook_exc:
                logger.error(f"Error in on_retry hook: {hook_exc}")

        before_sleep_log(logger, logging.WARNING)(retry_state)

    # Callback de sucesso se a execução passar
    # O tenacity não tem hook nativo pós-sucesso fácil, mas podemos tratar no decorator ou no retry wrapper.

    return tenacity.AsyncRetrying(
        stop=wrapped_stop,
        wait=wait_exponential(
            multiplier=config.min_wait,
            max=config.max_wait,
            exp_base=config.backoff_factor,
        ),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_cb,
        reraise=True,
    )


async def retry_call_async(
    func: Callable, config: RetryConfig, *args, **kwargs
) -> RetryResult:
    """Executa chamadas assíncronas com lógica de retry."""
    total_wait = 0.0
    last_exc = None

    max_attempts = config.max_attempts if config.max_attempts is not None else 3
    for attempt in range(1, max_attempts + 1):
        if config.circuit_breaker is not None:
            cb = config.circuit_breaker
            cb_state = getattr(cb, "state", None)
            if cb_state == "open" or (
                hasattr(cb_state, "value") and cb_state.value == "open"
            ):
                from src.utils.circuit_breaker import CircuitBreakerOpen

                raise CircuitBreakerOpen(
                    f"Circuit Breaker '{getattr(cb, 'name', 'unnamed')}' is OPEN. Abortando retries."
                )

        try:
            val = await func(*args, **kwargs)
            res = RetryResult(
                value=val, attempts=attempt, total_wait_seconds=total_wait
            )
            if config.circuit_breaker is not None and hasattr(
                config.circuit_breaker, "record_success"
            ):
                config.circuit_breaker.record_success()
            return res
        except Exception as exc:
            last_exc = exc

            if not any(
                isinstance(exc, expected) for expected in config.expected_exceptions
            ):
                if config.circuit_breaker is not None and hasattr(
                    config.circuit_breaker, "record_failure"
                ):
                    config.circuit_breaker.record_failure(str(exc))
                raise exc

            if attempt == config.max_attempts:
                if config.circuit_breaker is not None and hasattr(
                    config.circuit_breaker, "record_failure"
                ):
                    config.circuit_breaker.record_failure(str(exc))
                break

            wait_sec = calculate_backoff(attempt + 1, config)
            logger.warning(
                f"Attempt {attempt} for async call failed with exception: {exc}. "
                f"Retrying in {wait_sec:.2f}s..."
            )

            if config.on_retry is not None:
                try:
                    config.on_retry(attempt, exc, wait_sec)
                except Exception as hook_exc:
                    logger.error(f"Error in on_retry hook: {hook_exc}")

            await asyncio.sleep(wait_sec)
            total_wait += wait_sec

    res = RetryResult(
        value=None,
        attempts=config.max_attempts,
        total_wait_seconds=total_wait,
        last_exception=last_exc,
    )
    return res


def retry_call_sync(
    func: Callable, config: RetryConfig, *args, **kwargs
) -> RetryResult:
    """Executa chamadas síncronas com lógica de retry."""
    total_wait = 0.0
    last_exc = None

    max_attempts = config.max_attempts if config.max_attempts is not None else 3
    for attempt in range(1, max_attempts + 1):
        if config.circuit_breaker is not None:
            cb = config.circuit_breaker
            cb_state = getattr(cb, "state", None)
            if cb_state == "open" or (
                hasattr(cb_state, "value") and cb_state.value == "open"
            ):
                from src.utils.circuit_breaker import CircuitBreakerOpen

                raise CircuitBreakerOpen(
                    f"Circuit Breaker '{getattr(cb, 'name', 'unnamed')}' is OPEN. Abortando retries."
                )

        try:
            val = func(*args, **kwargs)
            res = RetryResult(
                value=val, attempts=attempt, total_wait_seconds=total_wait
            )
            if config.circuit_breaker is not None and hasattr(
                config.circuit_breaker, "record_success"
            ):
                config.circuit_breaker.record_success()
            return res
        except Exception as exc:
            last_exc = exc

            if not any(
                isinstance(exc, expected) for expected in config.expected_exceptions
            ):
                if config.circuit_breaker is not None and hasattr(
                    config.circuit_breaker, "record_failure"
                ):
                    config.circuit_breaker.record_failure(str(exc))
                raise exc

            if attempt == config.max_attempts:
                if config.circuit_breaker is not None and hasattr(
                    config.circuit_breaker, "record_failure"
                ):
                    config.circuit_breaker.record_failure(str(exc))
                break

            wait_sec = calculate_backoff(attempt + 1, config)
            logger.warning(
                f"Attempt {attempt} for sync call failed with exception: {exc}. "
                f"Retrying in {wait_sec:.2f}s..."
            )

            if config.on_retry is not None:
                try:
                    config.on_retry(attempt, exc, wait_sec)
                except Exception as hook_exc:
                    logger.error(f"Error in on_retry hook: {hook_exc}")

            time.sleep(wait_sec)
            total_wait += wait_sec

    res = RetryResult(
        value=None,
        attempts=config.max_attempts,
        total_wait_seconds=total_wait,
        last_exception=last_exc,
    )
    return res


async def retry_call(func: Callable, *args, **kwargs) -> Any:
    """Função direta (sem decorator) para retry dinâmico.

    Suporta parâmetros de RetryConfig passados via kwargs ou argumentos tradicionais.
    Se o retorno for do tipo RetryResult, retorna o valor real extraído de RetryResult.value.
    """
    # Extrai configs dos kwargs de controle de retry
    cfg_kwargs = {}
    for field_name in [
        "max_attempts",
        "min_wait",
        "max_wait",
        "backoff_factor",
        "jitter_ratio",
        "expected_exceptions",
        "circuit_breaker",
        "on_retry",
        "max_retries",
        "base_delay",
        "max_delay",
        "exponential_base",
        "jitter",
        "retryable_exceptions",
        "retry_on",
    ]:
        if field_name in kwargs:
            cfg_kwargs[field_name] = kwargs.pop(field_name)

    config = RetryConfig(**cfg_kwargs)

    if asyncio.iscoroutinefunction(func):
        res = await retry_call_async(func, config, *args, **kwargs)
    else:
        res = retry_call_sync(func, config, *args, **kwargs)

    log_retry_summary(func.__name__, res)

    if not res.success and res.last_exception is not None:
        raise res.last_exception

    return res.value


def retry_with_backoff(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 60.0,
    backoff_factor: float = 2.0,
    jitter_ratio: float = 0.25,
    expected_exceptions: Sequence[Type[Exception]] = (Exception,),
    circuit_breaker: Optional[Any] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
):
    """Decorator para retry com backoff exponencial e jitter.

    Funciona tanto para funções assíncronas (async def) quanto síncronas (def).
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        min_wait=min_wait,
        max_wait=max_wait,
        backoff_factor=backoff_factor,
        jitter_ratio=jitter_ratio,
        expected_exceptions=expected_exceptions,
        circuit_breaker=circuit_breaker,
        on_retry=on_retry,
    )

    def decorator(func: Callable):
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def wrapper(*args, **kwargs):
                res = await retry_call_async(func, config, *args, **kwargs)
                log_retry_summary(func.__name__, res)
                if not res.success and res.last_exception is not None:
                    raise res.last_exception
                return res.value

            return wrapper
        else:

            @wraps(func)
            def wrapper(*args, **kwargs):
                res = retry_call_sync(func, config, *args, **kwargs)
                log_retry_summary(func.__name__, res)
                if not res.success and res.last_exception is not None:
                    raise res.last_exception
                return res.value

            return wrapper

    return decorator


# ── Aliases de Compatibilidade Retroativa ───────────────────────────────────


def with_retry(config: RetryConfig | None = None):
    """Alias para with_retry legado (espera receber uma instância de RetryConfig)."""

    def decorator(func: Callable):
        cfg = config if config is not None else RetryConfig()

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def wrapper(*args, **kwargs):
                res = await retry_call_async(func, cfg, *args, **kwargs)
                log_retry_summary(func.__name__, res)
                if not res.success and res.last_exception is not None:
                    raise res.last_exception
                return res.value

            return wrapper
        else:

            @wraps(func)
            def wrapper(*args, **kwargs):
                res = retry_call_sync(func, cfg, *args, **kwargs)
                log_retry_summary(func.__name__, res)
                if not res.success and res.last_exception is not None:
                    raise res.last_exception
                return res.value

            return wrapper

    return decorator
