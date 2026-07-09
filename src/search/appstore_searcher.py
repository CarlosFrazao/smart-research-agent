"""AppStoreSearcher — Busca apps na App Store via iTunes Search API.

Endpoint: GET https://itunes.apple.com/search?term={query}&entity=software&limit=10&country=br

API pública, sem necessidade de chave. Busca por aplicativos iOS/macOS.
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.appstore")

APPSTORE_BASE_URL = "https://itunes.apple.com"
APPSTORE_COUNTRY = "br"


@register_searcher("appstore", enabled_env="SRA_APPSTORE_ENABLED")
class AppStoreSearcher(APISearcher):
    """Searcher de aplicativos na App Store via iTunes Search API.

    Busca por software (apps) na App Store, com suporte a filtros de
    país. Por padrão, busca por apps brasileiros (``country=br``).
    """

    def __init__(self, config: dict[str, Any]):
        country = config.get("appstore_country", APPSTORE_COUNTRY)
        api_config = APISearcherConfig(
            source_name="appstore",
            base_url=APPSTORE_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
        )
        super().__init__(api_config)
        self._country = country

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de apps na App Store.

        Args:
            query: Termo de busca (ex: "WhatsApp", "Notion").
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com apps encontrados.
        """
        params = {
            "term": query,
            "entity": "software",
            "limit": self.max_results,
            "country": self._country,
        }

        try:
            data = await self._make_request("GET", "/search", params=params)
            results = data.get("results", []) if isinstance(data, dict) else []
            parsed = [self.normalize(r) for r in results if isinstance(r, dict)]
            logger.debug(f"AppStoreSearcher: {len(parsed)} apps para '{query}'")
            return parsed[: self.max_results]
        except Exception as e:
            logger.warning(f"AppStoreSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um app da App Store para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(source="appstore", title="", url="", description="")

        price = raw_result.get("price", 0.0)
        price_label = "GratÃ¡tis" if price in (0, 0.0, None) else f"R$ {price:.2f}"

        description = raw_result.get("description", "")
        # Limita descriÃ§Ã£o para evitar textos muito longos
        if len(description) > 500:
            description = description[:500] + "..."

        return SearchResult(
            source="appstore",
            title=raw_result.get("trackName", ""),
            url=raw_result.get("trackViewUrl", ""),
            description=description,
            metrics={
                "average_user_rating": raw_result.get("averageUserRating", 0.0),
                "primary_genre": raw_result.get("primaryGenreName", ""),
                "price": price,
                "price_label": price_label,
                "artist": raw_result.get("artistName", ""),
                "release_date": raw_result.get("releaseDate", ""),
            },
            raw=raw_result,
        )