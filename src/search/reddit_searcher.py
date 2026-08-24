import logging
import os
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from src.clients.firecrawl_client import FirecrawlClient
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient
from src.utils.retry import RetryConfig, with_retry

logger = logging.getLogger(__name__)

_RETRY_CONFIG = RetryConfig(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    exponential_base=2.0,
    jitter=True,
    retryable_exceptions=(Exception,),
)

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
    """Buscador especializado para coletar e estruturar resultados vindos do Reddit."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.base_url = "https://www.reddit.com/search.json"
        self.http = HTTPClient(timeout=self.timeout)
        # Use Firecrawl client to bypass Reddit's bot detection
        self._firecrawl: FirecrawlClient | None = None
        fc_key = config.get("firecrawl_api_key", "")
        fc_url = config.get("firecrawl_base_url")
        if fc_key or fc_url:
            try:
                self._firecrawl = FirecrawlClient(api_key=fc_key, base_url=fc_url)
            except Exception as e:
                logger.warning(f"Reddit: Firecrawl nao disponivel: {e}")
        self.circuit = CircuitBreakerRegistry.get(
            "reddit_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(
        self, query: str, domain: str = "general", **kwargs
    ) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Reddit.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "reddit_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._search_pipeline, query, domain)
        except CircuitBreakerOpen as e:
            logger.warning(f"RedditSearcher: {e}")
            return self.fallback(query)

    @with_retry(_RETRY_CONFIG)
    async def _search_pipeline(self, query: str, domain: str) -> list[SearchResult]:
        # Simplifica a query para o Reddit removendo qualificadores do GitHub/metadados
        import re

        words = query.lower().split()
        ignore = {
            "github",
            "repos",
            "repositories",
            "repository",
            "implementing",
            "implemented",
            "stars",
            "star",
            "last",
            "days",
            "weeks",
            "months",
            "month",
            "week",
            "day",
            "with",
            "and",
            "or",
            "for",
            "in",
            "to",
            "at",
            "above",
            "more",
            "than",
            "least",
        }
        clean_words = []
        for w in words:
            # Remove pontuações comuns
            w_clean = re.sub(r"[^\w\+\-#]", "", w)
            if (
                w_clean
                and w_clean not in ignore
                and not w_clean.isdigit()
                and not w_clean.startswith(">")
            ):
                clean_words.append(w_clean)

        search_query = " ".join(clean_words) if clean_words else query
        logger.info(f"Reddit: simplificou query '{query[:40]}' -> '{search_query}'")

        # Strategy 1: Firecrawl-powered Reddit search (bypasses bot detection)
        results = await self._search_via_firecrawl(search_query, domain)
        if results:
            logger.info(
                f"Reddit via Firecrawl: {len(results)} resultados para '{search_query}'"
            )
            return results

        # Strategy 2: Direct JSON API with fresh session per request
        results = await self._search_direct_api(search_query, domain)
        if results:
            logger.info(
                f"Reddit via API direta: {len(results)} resultados para '{search_query}'"
            )
            return results

        # Strategy 3: Pushshift / alternative endpoint
        results = await self._search_pushshift(search_query)
        if results:
            logger.info(f"Reddit via Pushshift: {len(results)} resultados")
            return results

        # Strategy 4: SearXNG com site:reddit.com
        try:
            logger.info(
                f"Reddit: acionando Strategy 4 (SearXNG site:reddit.com) para '{search_query}'"
            )
            from src.search.searxng_searcher import SearXNGSearcher

            # Instancia localmente o SearXNG com o mesmo timeout
            searxng_cfg = {
                "timeout": self.timeout,
                "max_results": self.max_results,
                "searxng_url": os.getenv("SEARXNG_URL", "http://127.0.0.1:3023"),
                "searxng_engines": os.getenv(
                    "SEARXNG_ENGINES", "google,bing,duckduckgo"
                ),
                "searxng_categories": os.getenv("SEARXNG_CATEGORIES", "general"),
            }
            searxng = SearXNGSearcher(searxng_cfg)
            reddit_query = f"{search_query} site:reddit.com"
            s_results = await searxng.search(reddit_query)
            if s_results:
                results = []
                for r in s_results:
                    r.source = "reddit"
                    r.metrics["subreddit"] = self._extract_subreddit_from_url(r.url)
                    results.append(r)
                logger.info(
                    f"Reddit via SearXNG fallback: {len(results)} resultados para '{query[:40]}'"
                )
                return results[: self.max_results]
        except Exception as e:
            logger.debug(f"Reddit via SearXNG falhou: {e}")

        # Strategy 5: Jina Search com site:reddit.com (como fallback final de emergência)
        try:
            logger.info(
                f"Reddit: acionando Strategy 5 (Jina Search site:reddit.com) para '{search_query}'"
            )
            import httpx

            jina_search_url = f"https://s.jina.ai/{urllib.parse.quote(f'{search_query} site:reddit.com', safe='')}"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    jina_search_url,
                    headers={"Accept": "text/markdown"},
                    follow_redirects=True,
                )
                if resp.status_code == 200 and resp.text:
                    logger.info(
                        "Reddit via Jina Search: Sucesso ao obter busca pública."
                    )
                    content = resp.text
                    return [
                        SearchResult(
                            source="reddit",
                            title=f"Reddit Jina: {search_query[:40]}",
                            url=jina_search_url,
                            description=content[:1500],
                            metrics={
                                "subreddit": "search",
                                "source_domain": "s.jina.ai",
                            },
                            raw={"markdown": content},
                        )
                    ]
        except Exception as e:
            logger.debug(f"Reddit via Jina Search falhou: {e}")

        logger.warning(f"Reddit: todas as estratégias falharam para '{query[:50]}'")
        return self.fallback(query)

    @staticmethod
    def _extract_subreddit_from_url(url: str) -> str:
        import re

        m = re.search(r"reddit\.com/r/([^/]+)", url)
        return m.group(1) if m else "unknown"

    async def _search_via_firecrawl(
        self, query: str, domain: str
    ) -> list[SearchResult]:
        """Usa a API de busca nativa do Firecrawl com site:reddit.com para obter resultados do Reddit."""
        if not self._firecrawl:
            return []
        try:
            reddit_query = f"{query} site:reddit.com"
            raw_results = await self._firecrawl.search(reddit_query, limit=15)
            if not raw_results:
                # Foco self-hosted: o operador `site:` nem sempre é respeitado
                # pelo indexador do Firecrawl local. Fallback: busca semântica
                # com "reddit" explícito e filtro por domínio reddit.com.
                logger.info(
                    f"Reddit Firecrawl: site: vazio para '{query[:40]}'. "
                    "Tentando busca sem qualificador..."
                )
                raw_results = await self._firecrawl.search(f"reddit {query}", limit=15)
                if raw_results:
                    raw_results = [
                        r
                        for r in raw_results
                        if "reddit.com" in (r.get("url", "") or "")
                    ]

            if not raw_results:
                return []

            results = []
            for r in raw_results:
                title = r.get("title", "")
                url = r.get("url", "")
                markdown = r.get("markdown", "") or r.get("description", "") or ""

                results.append(
                    SearchResult(
                        source="reddit",
                        title=title,
                        url=url,
                        description=markdown[:500],
                        metrics={
                            "subreddit": self._extract_subreddit_from_url(url),
                            "subreddit_relevance": 10,
                        },
                        raw=r,
                    )
                )

            priority_subs = [s.lower() for s in TECH_SUBREDDITS.get(domain, [])]
            for r in results:
                sub = r.metrics.get("subreddit", "").lower()
                if sub in priority_subs:
                    r.metrics["subreddit_relevance"] = 25
            return results
        except Exception as e:
            logger.debug(f"Reddit Firecrawl Search falhou: {e}")
        return []

    async def _search_direct_api(self, query: str, domain: str) -> list[SearchResult]:
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
                    self.base_url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        posts = data.get("data", {}).get("children", [])
                        if posts:
                            results = [self.normalize(p["data"]) for p in posts]
                            priority_subs = [
                                s.lower() for s in TECH_SUBREDDITS.get(domain, [])
                            ]
                            for r in results:
                                sub = r.metrics.get("subreddit", "").lower()
                                r.metrics["subreddit_relevance"] = (
                                    25 if sub in priority_subs else 10
                                )
                            return results
                    else:
                        logger.warning(
                            f"Reddit API status {resp.status} para '{query[:40]}'"
                        )
        except Exception as e:
            logger.debug(f"Reddit API direta falhou: {e}")
        return []

    async def _search_pushshift(self, query: str) -> list[SearchResult]:
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
        published_at = None
        try:
            published_at = datetime.fromtimestamp(post.get("created_utc", 0), UTC)
        except (ValueError, OSError, OverflowError):
            published_at = None
        return SearchResult(
            source="reddit",
            title=post.get("title", "Sem titulo"),
            url=f"https://reddit.com{post.get('permalink', '')}",
            description=post.get("selftext", "")[:500] or post.get("url", ""),
            published_at=published_at,
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
        """Normaliza um resultado bruto vindo do Reddit para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        created = datetime.fromtimestamp(post.get("created_utc", 0)).isoformat()
        published_at = None
        try:
            published_at = datetime.fromtimestamp(post.get("created_utc", 0), UTC)
        except (ValueError, OSError, OverflowError):
            published_at = None
        return SearchResult(
            source="reddit",
            title=post.get("title", "Sem titulo"),
            url=f"https://reddit.com{post.get('permalink', '')}",
            description=post.get("selftext", "")[:500],
            published_at=published_at,
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
