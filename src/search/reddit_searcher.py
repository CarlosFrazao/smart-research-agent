from datetime import datetime
from typing import List, Dict, Any, Optional
import os
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
from src.clients.firecrawl_client import FirecrawlClient
import logging
import urllib.parse
import json

logger = logging.getLogger(__name__)

TECH_SUBREDDITS = {
    "saas_b2b": ["selfhosted", "SaaS", "startups", "webdev"],
    "dev_tools": ["programming", "webdev", "python", "javascript"],
    "ai_ml": ["MachineLearning", "LocalLLaMA", "artificial", "singularity"],
    "automation": ["selfhosted", "homeautomation", "programming"],
    "infrastructure": ["selfhosted", "docker", "kubernetes", "devops"],
    "open_source": ["selfhosted", "opensource", "programming"],
    "general": ["technology", "programming", "webdev"],
}

# Browser-like User-Agent
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class RedditSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = "https://www.reddit.com/search.json"
        self.http = HTTPClient(timeout=self.timeout)
        # Use Firecrawl client to bypass Reddit's bot detection
        self._firecrawl: Optional[FirecrawlClient] = None
        fc_key = config.get("firecrawl_api_key", "")
        fc_url = config.get("firecrawl_base_url")
        if fc_key or fc_url:
            try:
                self._firecrawl = FirecrawlClient(api_key=fc_key, base_url=fc_url)
            except Exception as e:
                logger.warning(f"Reddit: Firecrawl nao disponivel: {e}")

    async def search(self, query: str, domain: str = "general", **kwargs) -> List[SearchResult]:
        # Strategy 1: Firecrawl-powered Reddit search (bypasses bot detection)
        results = await self._search_via_firecrawl(query, domain)
        if results:
            logger.info(f"Reddit via Firecrawl: {len(results)} resultados para '{query[:40]}'")
            return results

        # Strategy 2: Direct JSON API with fresh session per request
        results = await self._search_direct_api(query, domain)
        if results:
            logger.info(f"Reddit via API direta: {len(results)} resultados para '{query[:40]}'")
            return results

        # Strategy 3: Pushshift / alternative endpoint
        results = await self._search_pushshift(query)
        if results:
            logger.info(f"Reddit via Pushshift: {len(results)} resultados")
            return results

        # Strategy 4: SearXNG com site:reddit.com
        try:
            logger.info(f"Reddit: acionando Strategy 4 (SearXNG site:reddit.com) para '{query[:40]}'")
            from src.search.searxng_searcher import SearXNGSearcher
            # Instancia localmente o SearXNG com o mesmo timeout
            searxng_cfg = {
                "timeout": self.timeout,
                "max_results": self.max_results,
                "searxng_url": os.getenv("SEARXNG_URL", "http://127.0.0.1:3023"),
                "searxng_engines": os.getenv("SEARXNG_ENGINES", "google,bing,duckduckgo"),
                "searxng_categories": os.getenv("SEARXNG_CATEGORIES", "general")
            }
            searxng = SearXNGSearcher(searxng_cfg)
            reddit_query = f"{query} site:reddit.com"
            s_results = await searxng.search(reddit_query)
            if s_results:
                results = []
                for r in s_results:
                    r.source = "reddit"
                    r.metrics["subreddit"] = self._extract_subreddit_from_url(r.url)
                    results.append(r)
                logger.info(f"Reddit via SearXNG fallback: {len(results)} resultados para '{query[:40]}'")
                return results[:self.max_results]
        except Exception as e:
            logger.debug(f"Reddit via SearXNG falhou: {e}")

        logger.warning(f"Reddit: todas as estratégias falharam para '{query[:50]}'")
        return self.fallback(query)

    @staticmethod
    def _extract_subreddit_from_url(url: str) -> str:
        import re
        m = re.search(r"reddit\.com/r/([^/]+)", url)
        return m.group(1) if m else "unknown"

    async def _search_via_firecrawl(self, query: str, domain: str) -> List[SearchResult]:
        """Use Firecrawl to scrape Reddit search results (bypasses bot detection)."""
        if not self._firecrawl:
            return []
        try:
            encoded = urllib.parse.quote(query)
            # Use the JSON API endpoint via Firecrawl's browser engine
            reddit_url = (
                f"https://www.reddit.com/search.json"
                f"?q={encoded}&sort=relevance&t=year&limit=15&type=link"
            )
            raw = await self._firecrawl.scrape(reddit_url, formats=["markdown"])
            if not raw:
                return []

            # Try to extract JSON from markdown content
            markdown = raw.get("markdown", "") or raw.get("content", "")
            if not markdown:
                return []

            # The firecrawl might return the JSON as text in markdown
            # Try to parse it
            start = markdown.find("{")
            if start == -1:
                return []
            try:
                data = json.loads(markdown[start:])
                posts = data.get("data", {}).get("children", [])
                results = [self.normalize(p["data"]) for p in posts if p.get("data")]
                priority_subs = [s.lower() for s in TECH_SUBREDDITS.get(domain, [])]
                for r in results:
                    sub = r.metrics.get("subreddit", "").lower()
                    r.metrics["subreddit_relevance"] = 25 if sub in priority_subs else 10
                return results
            except json.JSONDecodeError:
                pass
        except Exception as e:
            logger.debug(f"Reddit Firecrawl falhou: {e}")
        return []

    async def _search_direct_api(self, query: str, domain: str) -> List[SearchResult]:
        """Direct Reddit JSON API with browser-like headers and fresh session."""
        import aiohttp
        params = {
            "q": query,
            "sort": "relevance",
            "t": "year",
            "limit": min(self.max_results, 25),
            "restrict_sr": "false",
            "type": "link",
        }
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

        # Use fresh connector (no session reuse) to avoid fingerprinting
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    self.base_url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        posts = data.get("data", {}).get("children", [])
                        if posts:
                            results = [self.normalize(p["data"]) for p in posts]
                            priority_subs = [s.lower() for s in TECH_SUBREDDITS.get(domain, [])]
                            for r in results:
                                sub = r.metrics.get("subreddit", "").lower()
                                r.metrics["subreddit_relevance"] = 25 if sub in priority_subs else 10
                            return results
                    else:
                        logger.warning(f"Reddit API status {resp.status} para '{query[:40]}'")
        except Exception as e:
            logger.debug(f"Reddit API direta falhou: {e}")
        return []

    async def _search_pushshift(self, query: str) -> List[SearchResult]:
        """Pushshift as final fallback for Reddit data."""
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://api.pushshift.io/reddit/search/submission/?q={encoded}&size=10&sort=score"
            data = await self.http.get(url, headers={"User-Agent": _UA})
            posts = data.get("data", [])
            if posts:
                return [self._normalize_pushshift(p) for p in posts[:10]]
        except Exception as e:
            logger.debug(f"Pushshift falhou: {e}")
        return []

    def _normalize_pushshift(self, post: dict) -> SearchResult:
        created = datetime.fromtimestamp(post.get("created_utc", 0)).isoformat()
        return SearchResult(
            source="reddit",
            title=post.get("title", "Sem titulo"),
            url=f"https://reddit.com{post.get('permalink', '')}",
            description=post.get("selftext", "")[:500] or post.get("url", ""),
            metrics={
                "upvotes": post.get("score", 0),
                "comments": post.get("num_comments", 0),
                "subreddit": post.get("subreddit", ""),
                "created_at": created,
                "author": post.get("author", ""),
                "score": post.get("score", 0),
            },
            raw=post,
        )

    def normalize(self, post: dict) -> SearchResult:
        created = datetime.fromtimestamp(post.get("created_utc", 0)).isoformat()
        return SearchResult(
            source="reddit",
            title=post.get("title", "Sem titulo"),
            url=f"https://reddit.com{post.get('permalink', '')}",
            description=post.get("selftext", "")[:500],
            metrics={
                "upvotes": post.get("ups", 0),
                "comments": post.get("num_comments", 0),
                "subreddit": post.get("subreddit", ""),
                "created_at": created,
                "author": post.get("author", ""),
                "score": post.get("score", 0),
            },
            raw=post,
        )
