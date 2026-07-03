import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from serpapi import GoogleSearch as _SerpAPIGoogleSearch
    _SERPAPI_AVAILABLE = True
except ImportError:
    _SERPAPI_AVAILABLE = False
    _SerpAPIGoogleSearch = None
    logger.warning("serpapi lib nao instalada. pip install google-search-results")



class SerpAPISearcher:
    """
    Fallback de ultimo recurso usando SerpAPI (Google/Bing via API paga).
    Ativado apenas quando todos os outros scrapers falharem.
    Requer SERPAPI_API_KEY configurada no .env
    """

    def __init__(self, api_key: str, engine: str = "google", max_results: int = 10):
        self.api_key = api_key
        self.engine = engine
        self.max_results = max_results
        self._available = _SERPAPI_AVAILABLE and bool(api_key)
        if not self._available:
            logger.warning("SerpAPISearcher: desabilitado (chave ausente ou lib nao instalada)")

    async def search(self, query: str, **kwargs) -> list[Any]:
        """
        Executa busca via SerpAPI em um thread pool para nao bloquear o event loop.
        Retorna lista de dicts compativel com SearchResult.
        """
        if not self._available:
            return []
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, self._sync_search, query)
            logger.info(f"SerpAPISearcher: {len(results)} resultados para '{query}'")
            return results
        except Exception as e:
            logger.error(f"SerpAPISearcher: falha na busca '{query}': {e}")
            return []

    def _sync_search(self, query: str) -> list:
        """Chamada bloqueante ao SerpAPI (executada em thread pool)."""
        params = {
            "q": query,
            "api_key": self.api_key,
            "engine": self.engine,
            "num": self.max_results,
        }
        search = _SerpAPIGoogleSearch(params)
        results_raw = search.get_dict()
        organic = results_raw.get("organic_results", [])
        normalized = []
        for item in organic:
            normalized.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "content": item.get("snippet", ""),
                "source": "serpapi",
                "query": query,
                "_serpapi_position": item.get("position", 0),
            })
        return normalized

    @property
    def is_available(self) -> bool:
        return self._available