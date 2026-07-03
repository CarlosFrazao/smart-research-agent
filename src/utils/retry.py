"""
Retry com exponential backoff e jitter aleatório.
Uso como decorator: @with_retry(RetryConfig(max_retries=3))
"""
import asyncio
import random
import logging
from typing import Callable, TypeVar, Tuple, Type
from functools import wraps
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: Tuple[Type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )


def with_retry(config: RetryConfig = None):
    """Decorator para retry com exponential backoff."""
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exc = None
            for attempt in range(config.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except config.retryable_exceptions as e:
                    last_exc = e
                    if attempt == config.max_retries:
                        break
                    delay = min(
                        config.base_delay * (config.exponential_base ** attempt),
                        config.max_delay,
                    )
                    if config.jitter:
                        delay *= random.uniform(0.75, 1.25)
                    logger.warning(
                        f"{func.__name__} falhou (tentativa {attempt+1}/{config.max_retries+1}). "
                        f"Retry em {delay:.2f}s: {e}"
                    )
                    await asyncio.sleep(delay)
            raise last_exc
        return async_wrapper
    return decorator
