from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
import logging

logger = logging.getLogger(__name__)


class SpiderSearcher(BaseSearcher):
    """
    Ultra-fast scraper powered by Spider.cloud (Rust-based engine).
    Intended as a fallback when Firecrawl times out (>10s) or returns 429.
    Requires SPIDER_ENABLED=true and a valid SPIDER_API_KEY.
    """

    BASE_URL = "https://api.spider.cloud"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("spider_api_key", "")
        self.http = HTTPClient(timeout=self.timeout)

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """
        Crawls a URL using the Spider.cloud API and returns markdown content.
        The query parameter is treated as the target URL to crawl.
        """
        if not query.startswith(("http://", "https://")):
            logger.debug(f"SpiderSearcher: '{query[:50]}' não é URL, ignorando")
            return self.fallback(query)

        if not self.api_key:
            logger.warning("SpiderSearcher: SPIDER_API_KEY not set, skipping")
            return self.fallback(query)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": query,
            "limit": self.max_results,
            "return_format": "markdown",
        }

        try:
            data = await self.http.post(
                f"{self.BASE_URL}/crawl",
                headers=headers,
                json=payload,
            )
            items = data if isinstance(data, list) else data.get("results", [])
            results = [self.normalize(item) for item in items if item]
            logger.info(f"SpiderSearcher: {len(results)} results for '{query[:50]}'")
            return results
        except Exception as e:
            logger.error(f"SpiderSearcher error: {e}")
            return self.fallback(query)

    def normalize(self, raw: Any) -> SearchResult:
        """Converts a Spider.cloud API response item into a SearchResult."""
        if isinstance(raw, str):
            return SearchResult(
                source="spider.cloud",
                title="Spider Result",
                url="",
                description=raw[:300],
                metrics={},
                raw={"content": raw},
            )
        return SearchResult(
            source="spider.cloud",
            title=raw.get("metadata", {}).get("title", raw.get("url", "")),
            url=raw.get("url", ""),
            description=raw.get("content", raw.get("markdown", ""))[:300],
            metrics={
                "status": raw.get("status", 200),
            },
            raw=raw,
        )

    def fallback(self, query: str) -> List[SearchResult]:
        """Returns empty list — caller (orchestrator cascade) will try FirecrawlSearcher."""
        logger.warning(f"SpiderSearcher fallback activated for: {query[:50]}")
        return []
