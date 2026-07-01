from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
import logging

logger = logging.getLogger(__name__)


class SteelSearcher(BaseSearcher):
    """
    Browser-as-a-Service scraper powered by Steel.dev.
    Use when Firecrawl or Spider return empty/partial content on JS-heavy pages.
    Supports session management, proxy rotation, and stealth plugins.
    Requires STEEL_ENABLED=true and a valid STEEL_API_KEY.
    """

    BASE_URL = "https://api.steel.dev/v1"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("steel_api_key", "")
        self.base_url = config.get("steel_base_url", self.BASE_URL)
        self.http = HTTPClient(timeout=self.timeout)

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """
        Scrapes a URL using Steel.dev's Quick Actions API (/v1/scrape).
        The query parameter is treated as the target URL.
        use_proxy and solve_captcha are enabled by default for maximum compatibility.
        """
        if not query.startswith(("http://", "https://")):
            logger.debug(f"SteelSearcher: '{query[:50]}' não é URL, ignorando")
            return self.fallback(query)

        if not self.api_key:
            logger.warning("SteelSearcher: STEEL_API_KEY not set, skipping")
            return self.fallback(query)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": query,
            "use_proxy": True,
            "solve_captcha": True,
        }

        try:
            data = await self.http.post(
                f"{self.base_url}/scrape",
                headers=headers,
                json=payload,
            )
            result = self.normalize(data)
            if result.description:
                logger.info(f"SteelSearcher: content retrieved for '{query[:50]}'")
                return [result]
            logger.warning(f"SteelSearcher: empty content returned for '{query[:50]}'")
            return []
        except Exception as e:
            logger.error(f"SteelSearcher error: {e}")
            return self.fallback(query)

    def normalize(self, raw: Any) -> SearchResult:
        """Converts a Steel.dev API response into a SearchResult."""
        if not isinstance(raw, dict):
            return SearchResult(
                source="steel.dev",
                title="Steel Result",
                url="",
                description="",
                metrics={},
                raw={},
            )

        content = raw.get("content", raw.get("markdown", raw.get("text", "")))
        metadata = raw.get("metadata", {}) or {}

        return SearchResult(
            source="steel.dev",
            title=metadata.get("title", raw.get("url", "")),
            url=raw.get("url", ""),
            description=content[:300],
            metrics={
                "status": raw.get("status", 200),
                "solve_captcha": raw.get("solve_captcha", False),
            },
            raw=raw,
        )

    def fallback(self, query: str) -> List[SearchResult]:
        """
        Returns empty list with a warning.
        Does not propagate to further scrapers — caller (orchestrator cascade) handles that.
        """
        logger.warning(
            f"SteelSearcher fallback: page may not have been extracted. URL: {query[:80]}"
        )
        return []
