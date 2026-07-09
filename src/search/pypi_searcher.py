"""PyPISearcher — Busca pacotes Python no PyPI via API REST pública.

A API do PyPI permite buscas sem API key.
Endpoints:
- Busca: https://pypi.org/pypi/?$query
- Detalhes: https://pypi.org/pypi/$package_name/json

Quando a busca direta por termo é incerta, tenta usar a query como nome de pacote direto.
"""

from __future__ import annotations

import urllib.parse
import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.pypi")


@register_searcher("pypi", enabled_env="SRA_PYPI_ENABLED", trusted=True)
class PyPISearcher(APISearcher):
    """Searcher para PyPI via API REST pública.

    Busca pacotes Python e obtém resumo.
    Não requer API key - usa endpoints públicos do PyPI.
    Marca como confiável (trusted=True) porque as fontes do PyPI são oficiais.
    """

    def __init__(self, config: dict[str, Any]):
        api_config = APISearcherConfig(
            source_name="pypi",
            base_url="https://pypi.org",
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca no PyPI.

        Args:
            query: Termo de busca (pode ser nome de pacote).
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista de SearchResult.
        """
        results = []
        # 1. Tenta buscar por termo
        try:
            page_path = "/pypi"
            params = {"q": query}
            data = await self._make_request("GET", page_path, params=params)
            items = []
            if isinstance(data, dict):
                items = data.get("products", []) or data.get("data", [])
            elif isinstance(data, list):
                items = data

            for item in items[:self.max_results]:
                result = self._create_from_pypi_item(item)
                if result:
                    results.append(result)

            # Se a busca por termo retornou resultados vazios, tentar como nome de pacote direto
            if not results:
                logger.debug(f"PyPI: {len(results)} resultados para '{query}' via busca por termo")
                try:
                    result = await self._fetch_pypi_data(query)
                    if result:
                        results.append(result)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"PyPI search falhou para '{query}': {e}")

        return results

    async def _fetch_pypi_data(self, identifier: str) -> SearchResult | None:
        """Busca dados do PyPI por nome de pacote."""
        # Tenta como nome de pacote direto (sem frequência)
        path = f"/pypi/{identifier}"
        try:
            data = await self._make_request("GET", path, use_cache=True)
            if isinstance(data, dict):
                result = self._create_from_pypi_item(data)
                return result
        except Exception:
            pass
        return None

    def _create_from_pypi_item(self, item: Any) -> SearchResult | None:
        """Cria SearchResult a partir de item do JSON do PyPI."""
        if not isinstance(item, dict):
            return None

        # Tenta extrair nome do pacote do campo 'name'
        name = item.get("name", item.get("id", ""))
        if not name:
            return None

        title = name
        description = item.get("summary", item.get("description", ""))
        url = f"https://pypi.org/project/{name}/"

        # Campos extras preferidos
        metrics = {}
        if "version" in item:
            metrics["version"] = item["version"]
        if "author" in item:
            metrics["author"] = item["author"]
        if "license" in item:
            metrics["license"] = item["license"]

        return SearchResult(
            source="pypi",
            title=title,
            url=url,
            description=description,
            metrics=metrics,
        )

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza resultado (já parseado em _create_from_pypi_item)."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        return SearchResult(
            source="pypi",
            title="Resultado desconhecido",
            url="",
            description=str(raw_result),
        )