import logging
from typing import Any

from src.clients.firecrawl_client import FirecrawlClient
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class FirecrawlSearcher(BaseSearcher):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client = FirecrawlClient(
            api_key=config.get("firecrawl_api_key", ""),
            base_url=config.get("firecrawl_base_url"),
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        try:
            raw_results = await self.client.search(query, limit=self.max_results)
            return [self.normalize(r) for r in raw_results]
        except Exception as e:
            logger.error(f"FirecrawlSearcher erro: {e}")
            return self.fallback(query)

    def normalize(self, result: dict) -> SearchResult:
        return SearchResult(
            source="firecrawl",
            title=result.get("title", ""),
            url=result.get("url", ""),
            description=result.get("markdown", "")[:300],
            metrics={},
            raw=result,
        )
