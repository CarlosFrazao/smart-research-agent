"""CourtListenerSearcher — Busca jurídica via API pública do CourtListener.

Endpoint: GET https://www.courtlistener.com/api/rest/v4/search/?q={query}&type=o&format=json

Opcionalmente autentica com COURTLISTENER_API_TOKEN via header
``Authorization: Token <token>``. Sem o token, usa a API pública (que possui
um limite de taxa mais baixo).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.courtlistener")

COURTLISTENER_BASE_URL = "https://www.courtlistener.com"


@register_searcher("courtlistener", enabled_env="SRA_COURTLISTENER_ENABLED")
class CourtListenerSearcher(APISearcher):
    """Searcher de jurisprudência/processos via CourtListener REST API.

    Busca opiniões de tribunais (``type=o``) por relevância. Suporta autenticação
    opcional via COURTLISTENER_API_TOKEN.
    """

    def __init__(self, config: dict[str, Any]):
        token = os.getenv("COURTLISTENER_API_TOKEN") or config.get(
            "courtlistener_api_token"
        )
        default_headers = {"Authorization": f"Token {token}"} if token else None
        api_config = APISearcherConfig(
            source_name="courtlistener",
            base_url=COURTLISTENER_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
            default_headers=default_headers,
        )
        super().__init__(api_config)
        self._authenticated = bool(token)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de opiniões jurídicas no CourtListener.

        Args:
            query: Termo de busca (ex: nome de caso, estatuto).
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com casos encontrados.
        """
        params = {"q": query, "type": "o", "format": "json"}

        try:
            data = await self._make_request(
                "GET", "/api/rest/v4/search/", params=params
            )
            results = data.get("results", []) if isinstance(data, dict) else []
            parsed = [self.normalize(r) for r in results if isinstance(r, dict)]
            logger.debug(
                f"CourtListenerSearcher: {len(parsed)} resultados para '{query}'"
            )
            return parsed[: self.max_results]
        except Exception as e:
            logger.warning(f"CourtListenerSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado do CourtListener para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(
                source="courtlistener", title="", url="", description=""
            )

        case_name = raw_result.get("caseName", "")
        absolute_url = raw_result.get("absoluteUrl", "")
        if absolute_url and not absolute_url.startswith("http"):
            absolute_url = f"{COURTLISTENER_BASE_URL}{absolute_url}"
        snippet = raw_result.get("snippet", "")

        return SearchResult(
            source="courtlistener",
            title=case_name,
            url=absolute_url,
            description=snippet,
            metrics={
                "court": raw_result.get("court", ""),
                "docketNumber": raw_result.get("docketNumber", ""),
            },
            raw=raw_result,
        )
