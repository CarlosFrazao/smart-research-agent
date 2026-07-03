"""
Token Bucket rate limiter por domínio — previne sobrecarga de APIs externas.

Uso em qualquer searcher / http_client:
    from src.utils.rate_limiter import DomainRateLimiter
    await DomainRateLimiter.wait(url)
"""
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)


@dataclass
class RateLimit:
    requests_per_second: float = 2.0
    burst_size: int = 5


class TokenBucket:
    """Algoritmo Token Bucket com controle assíncrono de concorrência."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate            # Tokens adicionados por segundo
        self.capacity = capacity    # Capacidade máxima (burst)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Aguarda até que um token esteja disponível."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            # Reabastece tokens proporcionalmente ao tempo decorrido
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens < 1:
                wait_time = (1 - self.tokens) / self.rate
                logger.debug(f"TokenBucket throttling: aguardando {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1


# ---------------------------------------------------------------------------
# Configurações por domínio (req/s, burst)
# ---------------------------------------------------------------------------
DOMAIN_LIMITS: dict[str, RateLimit] = {
    "api.github.com":         RateLimit(requests_per_second=1.5, burst_size=5),
    "www.reddit.com":         RateLimit(requests_per_second=1.0, burst_size=3),
    "reddit.com":             RateLimit(requests_per_second=1.0, burst_size=3),
    "hn.algolia.com":         RateLimit(requests_per_second=2.0, burst_size=5),
    "news.ycombinator.com":   RateLimit(requests_per_second=2.0, burst_size=5),
    "arxiv.org":              RateLimit(requests_per_second=3.0, burst_size=10),
    "export.arxiv.org":       RateLimit(requests_per_second=3.0, burst_size=10),
    "api.stackexchange.com":  RateLimit(requests_per_second=2.0, burst_size=5),
    "producthunt.com":        RateLimit(requests_per_second=1.5, burst_size=4),
    "default":                RateLimit(requests_per_second=2.0, burst_size=5),
}


class DomainRateLimiter:
    """Singleton de buckets por domínio — compartilhado globalmente no processo."""

    _buckets: dict = defaultdict(lambda: None)

    @classmethod
    def _get_bucket(cls, domain: str) -> TokenBucket:
        if cls._buckets.get(domain) is None:
            limit = DOMAIN_LIMITS.get(domain, DOMAIN_LIMITS["default"])
            cls._buckets[domain] = TokenBucket(limit.requests_per_second, limit.burst_size)
        return cls._buckets[domain]

    @classmethod
    async def wait(cls, url: str) -> None:
        """Bloqueia assincronamente até que o domínio permita mais uma requisição."""
        try:
            domain = urlparse(url).netloc
            bucket = cls._get_bucket(domain)
            await bucket.acquire()
        except Exception as e:
            # Nunca bloquear a execução por falha no rate limiter
            logger.warning(f"DomainRateLimiter: erro inesperado para {url}: {e}")

    @classmethod
    def reset_all(cls) -> None:
        """Limpa todos os buckets — usado em testes."""
        cls._buckets.clear()
