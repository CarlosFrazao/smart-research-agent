"""
Exceções compartilhadas por todos os searchers.

Centralizar as exceções evita que cada searcher (github, reddit, hn, ...)
declare sua própria hierarquia de erros, o que antes tornava o tratamento
de erros no orquestrador (quem chama os 18 searchers) inconsistente.
"""

from __future__ import annotations


class SearcherError(Exception):
    """Erro base para qualquer falha ocorrida dentro de um searcher."""

    def __init__(self, source: str, message: str, *, cause: Exception | None = None):
        self.source = source
        self.cause = cause
        super().__init__(f"[{source}] {message}")


class CircuitBreakerOpenError(SearcherError):
    """Levantada quando o circuit breaker de uma fonte está aberto (fail-fast)."""

    def __init__(self, source: str, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            source,
            f"circuit breaker aberto; tente novamente em {retry_after_seconds:.1f}s",
        )


class RateLimitExceededError(SearcherError):
    """Levantada quando o rate limiter local recusa a chamada (sem esperar)."""

    def __init__(self, source: str, wait_seconds: float):
        self.wait_seconds = wait_seconds
        super().__init__(
            source, f"rate limit local excedido; aguarde {wait_seconds:.1f}s"
        )


class UpstreamHTTPError(SearcherError):
    """Levantada quando a fonte externa responde com status de erro (4xx/5xx)."""

    def __init__(self, source: str, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(source, f"HTTP {status_code} ao acessar {url}")


class ParseError(SearcherError):
    """Levantada quando a resposta da fonte não pôde ser interpretada."""
