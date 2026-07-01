from typing import List, Dict, Any
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

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": min(self.max_results, 100),
        }

        try:
            data = await self.http.get(self.base_url, params=params)
            hits = data.get("hits", [])
            return [self.normalize(hit) for hit in hits]
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
