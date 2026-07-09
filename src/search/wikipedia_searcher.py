"""WikipediaSearcher — Busca na Wikipedia via API REST pública.

API da Wikipedia permite busca e resumo sem necessidade de API key.
Endpoint: https://{lang}.wikipedia.org/w/api.php

Exemplo de uso:
    @register_searcher("wikipedia", enabled_env="SRA_WIKIPEDIA_ENABLED", trusted=True)
    class WikipediaSearcher(APISearcher):
        ...
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.wikipedia")


@register_searcher("wikipedia", enabled_env="SRA_WIKIPEDIA_ENABLED", trusted=True)
class WikipediaSearcher(APISearcher):
    """Searcher para Wikipedia via API REST pública.

    Busca artigos na Wikipedia e retorna título, URL e resumo.
    Não requer API key - usa API pública gratuita.

    Attributes:
        lang: Idioma da Wikipedia (ex: "en", "pt", "es").
    """

    def __init__(self, config: dict[str, Any]):
        # Extrai configurações específicas
        self.lang = config.pop("lang", "en")
        self._base_url = f"https://{self.lang}.wikipedia.org"

        # Configuração do APISearcher
        api_config = APISearcherConfig(
            source_name="wikipedia",
            base_url=self._base_url,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,  # Wikipedia é estável, sem CB necessário
            cache_ttl=config.get("cache_ttl", 3600),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca na Wikipedia.

        Args:
            query: Termo de busca.
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com artigos encontrados.
        """
        # Endpoint de busca da Wikipedia
        search_path = "w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": self.max_results,
            "format": "json",
            "srlang": self.lang,
        }

        try:
            data = await self._make_request("GET", search_path, params=params)
            results = []
            for item in data.get("query", {}).get("search", []):
                result = self.normalize(item)
                results.append(result)
            logger.debug(f"WikipediaSearcher: {len(results)} resultados para '{query}'")
            return results
        except Exception as e:
            logger.warning(f"WikipediaSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza resultado da Wikipedia para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(
                source="wikipedia",
                title="Resultado inválido",
                url="",
                description="",
            )

        title = raw_result.get("title", "Sem título")
        pageid = raw_result.get("pageid", 0)
        url = f"{self._base_url}/wiki/{title.replace(' ', '_')}"
        description = raw_result.get("snippet", raw_result.get("excerpt", ""))

        return SearchResult(
            source="wikipedia",
            title=title,
            url=url,
            description=description,
            metrics={
                "pageid": pageid,
                "wordcount": raw_result.get("wordcount", 0),
            },
        )