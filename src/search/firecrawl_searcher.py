from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.clients.firecrawl_client import FirecrawlClient
import logging

logger = logging.getLogger(__name__)


class FirecrawlSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.client = FirecrawlClient(
            api_key=config.get("firecrawl_api_key", ""),
            base_url=config.get("firecrawl_base_url"),
        )

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
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
