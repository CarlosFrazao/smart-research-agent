from typing import List, Dict, Any
import asyncio
import time
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
import logging

logger = logging.getLogger(__name__)


class HNSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = "https://hn.algolia.com/api/v1/search"
        self.http = HTTPClient(timeout=self.timeout)
        self.last_request_time = 0.0
        self.min_interval = 3.6  # segundos entre requests
        self._cache: Dict[str, List[SearchResult]] = {}

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        cache_key = f"{query}:{self.max_results}"
        if cache_key in self._cache:
            logger.info(f"HN search cache hit para: '{query}'")
            return self._cache[cache_key]

        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            logger.info(f"HN search rate-limit throttle: aguardando {wait_time:.2f}s")
            await asyncio.sleep(wait_time)

        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": min(self.max_results, 100),
        }

        try:
            data = await self.http.get(self.base_url, params=params)
            hits = data.get("hits", [])
            results = [self.normalize(hit) for hit in hits]
            self.last_request_time = time.time()
            self._cache[cache_key] = results
            return results
        except Exception as e:
            logger.error(f"HN search erro: {e}")
            return self.fallback(query)

    def normalize(self, hit: dict) -> SearchResult:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        return SearchResult(
            source="hackernews",
            title=hit.get("title", "Sem titulo"),
            url=url,
            description=hit.get("story_text", "")[:500],
            metrics={
                "points": hit.get("points", 0),
                "comments": hit.get("num_comments", 0),
                "author": hit.get("author", ""),
                "created_at": hit.get("created_at", ""),
                "object_id": hit.get("objectID", ""),
            },
            raw=hit,
        )
