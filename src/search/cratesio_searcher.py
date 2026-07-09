"""CratesIOSearcher — Busca crates (pacotes Rust) no crates.io.

A API pública do crates.io não exige chave e requer um User-Agent customizado:
  GET https://crates.io/api/v1/crates?q={query}&per_page={per_page}

A resposta traz ``crates[]`` com ``name``, ``description``, ``homepage``,
``repository`` e ``newest_version``. A fonte é marcada como confiável
(``trusted=True``) pois o registro oficial retorna metadados estruturados.
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.cratesio")

CRATESIO_BASE_URL = "https://crates.io"
CRATESIO_API_PATH = "/api/v1/crates"
CRATESIO_USER_AGENT = "smart-research-agent/1.0"


@register_searcher("cratesio", enabled_env="SRA_CRATESIO_ENABLED", trusted=True)
class CratesIOSearcher(APISearcher):
    """Searcher para o registro público do crates.io.

    Busca crates (pacotes Rust) e retorna metadados estruturados. Exige o
    header ``User-Agent: smart-research-agent/1.0`` imposto pelo crates.io.

    Attributes:
        base_url: URL base do crates.io.
    """

    def __init__(self, config: dict[str, Any]):
        api_config = APISearcherConfig(
            source_name="cratesio",
            base_url=CRATESIO_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
            # Header obrigatório do crates.io
            default_headers={"User-Agent": CRATESIO_USER_AGENT},
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de crates no crates.io.

        Args:
            query: Termo de busca (nome ou palavra-chave).
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista de SearchResult com crates encontradas.
        """
        per_page = min(self.max_results, 100)
        params = {"q": query, "per_page": per_page}

        try:
            data = await self._make_request("GET", CRATESIO_API_PATH, params=params)
            results: list[SearchResult] = []
            crates = data.get("crates", []) if isinstance(data, dict) else []
            for crate in crates:
                if not isinstance(crate, dict):
                    continue
                result = self.normalize(crate)
                if result and result.title:
                    results.append(result)
            logger.debug(f"CratesIOSearcher: {len(results)} resultados para '{query}'")
            return results[: self.max_results]
        except Exception as e:
            logger.warning(f"CratesIOSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult | None:
        """Converte uma crate da resposta do crates.io em SearchResult."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        if not isinstance(raw_result, dict):
            return None

        name = raw_result.get("name", "")
        if not name:
            return None

        description = raw_result.get("description", "") or ""
        homepage = raw_result.get("homepage") or ""
        repository = raw_result.get("repository") or ""
        version = raw_result.get("newest_version", "") or raw_result.get("max_version", "")
        url = homepage or repository or f"https://crates.io/crates/{name}"

        metrics: dict[str, Any] = {
            "version": version,
            "homepage": homepage,
            "repository": repository,
        }
        if raw_result.get("downloads") is not None:
            metrics["downloads"] = raw_result["downloads"]
        if raw_result.get("recent_downloads") is not None:
            metrics["recent_downloads"] = raw_result["recent_downloads"]

        return SearchResult(
            source="cratesio",
            title=name,
            url=url,
            description=description,
            metrics=metrics,
        )
