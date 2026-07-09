"""NPMSearcher — Busca pacotes no registro público do npm.

A API de busca do npm é pública e não exige chave:
  GET https://registry.npmjs.org/-/v1/search?text={query}&size={size}

A resposta traz ``objects[].package`` com ``name``, ``description``,
``links.npm``, ``version`` e ``keywords``. A fonte é marcada como confiável
(``trusted=True``) pois o registro oficial do npm retorna metadados
estruturados — não está em ``UNTRUSTED_SOURCES``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.npm")

NPM_SEARCH_BASE_URL = "https://registry.npmjs.org"
NPM_SEARCH_PATH = "/-/v1/search"


@register_searcher("npm", enabled_env="SRA_NPM_ENABLED", trusted=True)
class NPMSearcher(APISearcher):
    """Searcher para o registro público do npm.

    Busca pacotes Node.js e retorna metadados estruturados (nome, descrição,
    versão, keywords, link). Não requer API key.

    Attributes:
        base_url: URL base da API de busca do npm.
    """

    def __init__(self, config: dict[str, Any]):
        api_config = APISearcherConfig(
            source_name="npm",
            base_url=NPM_SEARCH_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de pacotes no npm.

        Args:
            query: Termo de busca (nome ou palavra-chave).
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista de SearchResult com pacotes encontrados.
        """
        size = min(self.max_results, 25)
        params = {"text": query, "size": size}

        try:
            data = await self._make_request("GET", NPM_SEARCH_PATH, params=params)
            results: list[SearchResult] = []
            objects = data.get("objects", []) if isinstance(data, dict) else []
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                package = obj.get("package", {})
                result = self.normalize(package)
                if result and result.title:
                    results.append(result)
            logger.debug(f"NPMSearcher: {len(results)} resultados para '{query}'")
            return results[: self.max_results]
        except Exception as e:
            logger.warning(f"NPMSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult | None:
        """Converte um pacote da resposta do npm em SearchResult."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        if not isinstance(raw_result, dict):
            return None

        name = raw_result.get("name", "")
        if not name:
            return None

        description = raw_result.get("description", "")
        links = raw_result.get("links", {}) or {}
        url = links.get("npm") or f"https://www.npmjs.com/package/{name}"
        version = raw_result.get("version", "")
        keywords = raw_result.get("keywords", []) or []

        metrics: dict[str, Any] = {"version": version, "keywords": keywords}
        if raw_result.get("publisher"):
            metrics["publisher"] = raw_result["publisher"]

        return SearchResult(
            source="npm",
            title=name,
            url=url,
            description=description,
            metrics=metrics,
        )
