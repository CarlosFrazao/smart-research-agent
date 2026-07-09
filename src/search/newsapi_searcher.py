"""NewsAPISearcher — Busca de notícias em tempo real via NewsAPI.org.

Endpoint: GET https://newsapi.org/v2/everything?q={query}&sortBy=relevancy&pageSize=10&apiKey={key}

A API key é lida da variável de ambiente NEWSAPI_KEY. Se não estiver configurada,
o searcher retorna lista vazia sem levantar erro (graceful degradation), pois a
fonte não pode ser consultada sem a chave.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.newsapi")

NEWSAPI_BASE_URL = "https://newsapi.org"
NEWSAPI_PAGE_SIZE = 10


@register_searcher("newsapi", requires_key="NEWSAPI_KEY")
class NewsAPISearcher(APISearcher):
    """Searcher de notícias em tempo real via NewsAPI.org.

    Usa o endpoint /v2/everything para buscar artigos ordenados por relevância.
    Requer a variável de ambiente NEWSAPI_KEY. Sem a chave, ``search()`` retorna
    ``[]`` de forma controlada (graceful).
    """

    def __init__(self, config: dict[str, Any]):
        # Lê a chave em tempo de construção; também suporta override via config.
        self._api_key = os.getenv("NEWSAPI_KEY") or config.get("newsapi_key")
        api_config = APISearcherConfig(
            source_name="newsapi",
            base_url=NEWSAPI_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", NEWSAPI_PAGE_SIZE),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 1800),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de notícias na NewsAPI.

        Args:
            query: Termo de busca.
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com artigos encontrados. Vazia se a chave
            NEWSAPI_KEY não estiver configurada.
        """
        if not self._api_key:
            logger.info(
                "NewsAPISearcher: NEWSAPI_KEY não configurada — retornando [] (graceful)."
            )
            return []

        params = {
            "q": query,
            "sortBy": "relevancy",
            "pageSize": self.max_results,
            "apiKey": self._api_key,
        }

        try:
            data = await self._make_request("GET", "/v2/everything", params=params)
            articles = data.get("articles", []) if isinstance(data, dict) else []
            results = [self.normalize(a) for a in articles if isinstance(a, dict)]
            logger.debug(f"NewsAPISearcher: {len(results)} artigos para '{query}'")
            return results[: self.max_results]
        except Exception as e:
            logger.warning(f"NewsAPISearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um artigo da NewsAPI para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(source="newsapi", title="", url="", description="")

        title = raw_result.get("title", "")
        url = raw_result.get("url", "")
        description = raw_result.get("description", "")

        source_info = raw_result.get("source", {})
        publisher = source_info.get("name", "") if isinstance(source_info, dict) else ""
        published_at = raw_result.get("publishedAt", "")

        return SearchResult(
            source="newsapi",
            title=title,
            url=url,
            description=description,
            metrics={
                "publisher": publisher,
                "published_at": published_at,
            },
            raw=raw_result,
        )
