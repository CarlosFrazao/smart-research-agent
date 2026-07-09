"""MercadoLivreSearcher — Busca produtos no MercadoLivre API.

Endpoint: GET https://api.mercadolibre.com/sites/MLB/search?q={query}&limit=10

Funciona com foco no Brasil — country "BR" embutido. Telefona para a fonte
"mercadolivre" ser listada como ``trusted=False`` para passar pelo
LLMSanitizer.
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.mercadolivre")

MERCADOLIVRE_BASE_URL = "https://api.mercadolibre.com"
MERCADOLIVRE_SITE = "MLB"


@register_searcher("mercadolivre", enabled_env="SRA_MERCADOLIVRE_ENABLED", trusted=False)
class MercadoLivreSearcher(APISearcher):
    """Searcher de produtos do MercadoLivre via API pública.

    Busca produtos por query retornando título, permalink, preço, condição e
    thumbnail. Fonte não-confiável (trusted=False): descrições de vendedores
    são texto livre e passam pelo LLMSanitizer em search_stage.py.
    """

    # Símbolos de moeda para formatar o preço de forma legível.
    CURRENCY_SYMBOLS = {
        "BRL": "R$",
        "USD": "US$",
        "ARS": "AR$",
        "MXN": "MX$",
        "CLP": "CL$",
        "COP": "CO$",
        "EUR": "€",
        "PYG": "₲",
    }

    def __init__(self, config: dict[str, Any]):
        site = config.get("ml_site", MERCADOLIVRE_SITE)
        self._search_path = f"/sites/{site}/search"
        api_config = APISearcherConfig(
            source_name="mercadolivre",
            base_url=MERCADOLIVRE_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 1800),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de produtos no MercadoLivre.

        Args:
            query: Termo de busca (ex: "fone bluetooth").
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com produtos encontrados.
        """
        params = {"q": query, "limit": self.max_results}
        try:
            data = await self._make_request("GET", self._search_path, params=params)
            results = data.get("results", []) if isinstance(data, dict) else []
            parsed = [self.normalize(r) for r in results if isinstance(r, dict)]
            logger.debug(
                f"MercadoLivreSearcher: {len(parsed)} produtos para '{query}'"
            )
            return parsed[: self.max_results]
        except Exception as e:
            logger.warning(f"MercadoLivreSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um produto do MercadoLivre para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(source="mercadolivre", title="", url="", description="")

        price = raw_result.get("price", "")
        currency = raw_result.get("currency_id", "")
        condition = raw_result.get("condition", "")
        seller_address = raw_result.get("seller_address", {}) or {}
        city = seller_address.get("city")
        if isinstance(city, dict):
            city = city.get("name", "")
        city = city or ""

        symbol = self.CURRENCY_SYMBOLS.get(currency, currency or "")
        if price not in ("", None):
            description = f"{symbol} {price} ({condition}) — {city}".strip()
        else:
            description = f"({condition}) — {city}".strip(" —")

        return SearchResult(
            source="mercadolivre",
            title=raw_result.get("title", ""),
            url=raw_result.get("permalink", ""),
            description=description,
            metrics={
                "price": price,
                "currency_id": currency,
                "condition": condition,
                "thumbnail": raw_result.get("thumbnail", ""),
                "city": city,
            },
            raw=raw_result,
        )
