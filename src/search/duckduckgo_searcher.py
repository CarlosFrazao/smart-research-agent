"""DuckDuckGoSearcher — Busca via API Instant Answer gratuita.

A API da DuckDuckGo permite buscas sem API key.
Endpoint: https://api.duckduckgo.com/?q={query}&format=json

Exemplo de uso:
    @register_searcher("duckduckgo", enabled_env="SRA_DUCKDUCKGO_ENABLED", trusted=False)
    class DuckDuckGoSearcher(APISearcher):
        ...
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.duckduckgo")


@register_searcher("duckduckgo", enabled_env="SRA_DUCKDUCKGO_ENABLED", trusted=False)
class DuckDuckGoSearcher(APISearcher):
    """Searcher para DuckDuckGo via API Instant Answer.

    Busca resultados instantâneos via DuckDuckGo API pública.
    Não requer API key - usa endpoint aberto.
    Marca como fonte não confiável (trusted=False) devido a risco de scraping.
    """

    def __init__(self, config: dict[str, Any]):
        api_config = APISearcherConfig(
            source_name="duckduckgo",
            base_url="https://api.duckduckgo.com",
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 1800),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca via DuckDuckGo Instant Answer API.

        Args:
            query: Termo de busca.
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista de SearchResult.
        """
        params = {
            "q": query,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1,
        }

        try:
            data = await self._make_request("GET", "/", params=params)
            results = self._parse_response(data)
            logger.debug(
                f"DuckDuckGoSearcher: {len(results)} resultados para '{query}'"
            )
            return results
        except Exception as e:
            logger.warning(f"DuckDuckGoSearcher falhou para '{query}': {e}")
            return []

    def _parse_response(self, data: dict[str, Any]) -> list[SearchResult]:
        """Parseia resposta JSON da DuckDuckGo em SearchResult."""
        results = []
        related_topics = data.get("RelatedTopics", [])

        if not related_topics:
            # Tenta usar AbstractText como fallback
            abstract = data.get("AbstractText", "")
            if abstract:
                results.append(
                    SearchResult(
                        source="duckduckgo",
                        title=data.get("Abstract", "Resultado DuckDuckGo"),
                        url=data.get("AbstractURL", ""),
                        description=abstract,
                    )
                )
            return results

        # Processa RelatedTopics (pode ser lista ou dict com sub-results)
        for topic in related_topics[: self.max_results]:
            if isinstance(topic, dict):
                if "Topics" in topic:
                    # Subcategorias aninhadas
                    for sub_topic in topic["Topics"][:5]:
                        result = self._create_from_topic(sub_topic)
                        if result:
                            results.append(result)
                else:
                    result = self._create_from_topic(topic)
                    if result:
                        results.append(result)

        return results

    def _create_from_topic(self, topic: dict[str, Any]) -> SearchResult | None:
        """Cria SearchResult a partir de um tópico da resposta."""
        if not topic:
            return None
        return SearchResult(
            source="duckduckgo",
            title=topic.get("Text", "Sem título")[:100],  # Limita título
            url=topic.get("FirstURL", ""),
            description=topic.get("Text", "")[:500],
            metrics={"source": "related_topics"},
        )

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza resultado (já parseado em _parse_response)."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        return SearchResult(
            source="duckduckgo",
            title="Resultado",
            url=raw_result.get("url", "") if isinstance(raw_result, dict) else "",
            description=str(raw_result),
        )
