"""Searcher do Hacker News via Algolia API.

Busca stories no Hacker News com rate-limiting, cache em memoria,
circuit breaker e retry automatico.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient
from src.utils.retry import RetryConfig, with_retry

logger = logging.getLogger(__name__)

_RETRY_CONFIG = RetryConfig(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    exponential_base=2.0,
    jitter=True,
    retryable_exceptions=(Exception,),
)


class HNSearcher(BaseSearcher):
    """Busca stories no Hacker News via Algolia API com circuit breaker e cache.

    Implementa rate-limiting de 3.6s entre requests para respeitar limites da API
    e usa circuit breaker para evitar sobrecarga em caso de falhas consecutivas.
    """

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.base_url = "https://hn.algolia.com/api/v1/search"
        self.http = HTTPClient(timeout=self.timeout)
        self.last_request_time = 0.0
        self.min_interval = 3.6  # segundos entre requests
        self._cache: dict[str, list[SearchResult]] = {}
        self.circuit = CircuitBreakerRegistry.get(
            "hn_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Busca stories no Hacker News para a query fornecida.

        Usa cache em memoria para evitar requests duplicados. Delega para
        o circuit breaker que controla o `_do_search` com retry automatico.

        Args:
            query: Texto da query de busca.
            **kwargs: Parametros extras (nao utilizados).

        Returns:
            list[SearchResult]: Stories do HN normalizados.
        """
        cache_key = f"{query}:{self.max_results}"
        if cache_key in self._cache:
            logger.info(f"HN search cache hit para: '{query}'")
            return self._cache[cache_key]

        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "hn_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._do_search, query, cache_key)
        except CircuitBreakerOpen as e:
            logger.warning(f"HNSearcher: {e}")
            return self.fallback(query)

    @with_retry(_RETRY_CONFIG)
    async def _do_search(self, query: str, cache_key: str) -> list[SearchResult]:
        """Executa a requisicao HTTP a API do Algolia HN com rate-limiting.

        Args:
            query: Texto da query de busca.
            cache_key: Chave para armazenar o resultado em cache.

        Returns:
            list[SearchResult]: Resultados normalizados ou lista vazia em erro.
        """
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            logger.info(f"HN search rate-limit throttle: aguardando {wait_time:.2f}s")
            await asyncio.sleep(wait_time)

        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": min(self.max_results, 100),
        }

        try:
            data = await self.http.get(self.base_url, params=params)
            hits = data.get("hits", [])
            results = [self.normalize(hit) for hit in hits]
            self.last_request_time = time.time()
            self._cache[cache_key] = results
            return results
        except Exception as e:
            logger.error(f"HN search erro: {e}")
            return self.fallback(query)

    def normalize(self, hit: dict) -> SearchResult:
        """Normaliza um hit da API Algolia HN para o formato `SearchResult`.

        Args:
            hit: Objeto de hit retornado pela API Algolia do HN.

        Returns:
            SearchResult: Resultado normalizado com metricas de pontos e comentarios.
        """
        url = (
            hit.get("url")
            or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        )
        # O Algolia HN retorna created_at como string ISO-8601 (UTC).
        created_at_raw = hit.get("created_at", "")
        published_at = None
        if created_at_raw:
            try:
                published_at = datetime.fromisoformat(
                    str(created_at_raw).replace("Z", "+00:00")
                )
            except Exception:
                published_at = None
        return SearchResult(
            source="hackernews",
            title=hit.get("title", "Sem titulo"),
            url=url,
            description=hit.get("story_text", "")[:500],
            published_at=published_at,
            metrics={
                "points": hit.get("points", 0),
                "comments": hit.get("num_comments", 0),
                "author": hit.get("author", ""),
                "created_at": created_at_raw,
                "object_id": hit.get("objectID", ""),
            },
            raw=hit,
        )
