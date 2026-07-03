"""Fábrica de instanciação dinâmica de searchers do Smart Research Agent.

Centraliza a criacao e configuracao de todos os searchers disponíveis,
permitindo que o orquestrador seja inicializado sem imports acoplados.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class SearcherFactory:
    """Fábrica estática para instanciar os searchers do ecossistema de pesquisa.

    Usa lazy imports para evitar dependências circulares e acelerar o boot
    do orquestrador. Cada searcher recebe o dicionário de configuracao
    extraído do `Config` do orquestrador.
    """

    @staticmethod
    def create_searchers(orchestrator: Any) -> dict[str, Any]:
        """Instancia dinamicamente todos os searchers configurados no sistema.

        Realiza lazy imports dos searchers para evitar dependências circulares
        e reduzir o tempo de boot. Searchers opcionais (ProductHunt, Firecrawl,
        Spider, Steel, SerpAPI) só são registrados se suas respectivas chaves de
        API estiverem configuradas.

        Args:
            orchestrator: Instância do `Orchestrator` com acesso ao `Config`.

        Returns:
            dict[str, Any]: Mapa de nome-do-searcher para instancia do searcher.
        """
        cfg = {
            "timeout": orchestrator.config.timeout_per_source,
            "max_results": orchestrator.config.max_results_per_source,
            "github_token": orchestrator.config.github_token,
            "producthunt_token": orchestrator.config.producthunt_token,
            "firecrawl_api_key": orchestrator.config.firecrawl_api_key,
            "firecrawl_base_url": orchestrator.config.firecrawl_base_url,
            "spider_api_key": orchestrator.config.spider_api_key,
            "spider_base_url": orchestrator.config.spider_base_url,
            "enabled": True,
            "steel_api_key": orchestrator.config.steel_api_key,
            "steel_base_url": orchestrator.config.steel_base_url,
        }

        # Lazy imports dos searchers para evitar dependências circulares e acelerar o boot
        from src.search.arxiv_searcher import ArxivSearcher
        from src.search.awesome_searcher import AwesomeSearcher
        from src.search.github_searcher import GitHubSearcher
        from src.search.hn_searcher import HNSearcher
        from src.search.reddit_searcher import RedditSearcher
        from src.search.rss_searcher import RSSSearcher
        from src.search.searxng_searcher import SearXNGSearcher
        from src.search.stackoverflow_searcher import StackOverflowSearcher
        from src.search.wayback_searcher import WaybackSearcher
        from src.search.web_searcher import WebSearcher
        from src.search.youtube_searcher import YouTubeSearcher
        from src.search.semantic_scholar_searcher import SemanticScholarSearcher
        from src.search.pubmed_searcher import PubMedSearcher

        searxng_cfg = {
            **cfg,
            "searxng_url": os.getenv("SEARXNG_URL", "http://127.0.0.1:3023"),
            "searxng_engines": os.getenv("SEARXNG_ENGINES", "google,bing,duckduckgo"),
            "searxng_categories": os.getenv("SEARXNG_CATEGORIES", "general"),
        }

        searchers: dict[str, Any] = {
            "github": GitHubSearcher(cfg),
            "reddit": RedditSearcher(cfg),
            "hackernews": HNSearcher(cfg),
            "awesome": AwesomeSearcher(cfg),
            "arxiv": ArxivSearcher(cfg),
            "web": WebSearcher(cfg),
            "rss": RSSSearcher({**cfg, "enabled": True}),
            "searxng": SearXNGSearcher(searxng_cfg),
            "stackoverflow": StackOverflowSearcher(cfg),
            "wayback": WaybackSearcher(cfg),
        }

        # ProductHunt se disponível
        try:
            from src.search.producthunt_searcher import ProductHuntSearcher

            searchers["producthunt"] = ProductHuntSearcher(cfg)
        except ImportError:
            logger.warning("ProductHuntSearcher não pôde ser importado")

        # Firecrawl / Jina
        if getattr(orchestrator.config, "host_mode", False):
            logger.info(
                "HOST MODE ativo — Firecrawl substituido por JinaSearcher como fallback"
            )
            from src.search.jina_searcher import JinaSearcher

            jina_cfg = {
                **cfg,
                "jina_base_url": getattr(
                    orchestrator.config, "jina_reader_base_url", "https://r.jina.ai/"
                ),
            }
            searchers["firecrawl"] = JinaSearcher(jina_cfg)
        else:
            from src.search.firecrawl_searcher import FirecrawlSearcher

            searchers["firecrawl"] = FirecrawlSearcher(cfg)

        # Spider
        if orchestrator.config.spider_enabled:
            from src.search.spider_searcher import SpiderSearcher

            searchers["spider"] = SpiderSearcher(cfg)

        # Steel
        if orchestrator.config.steel_enabled:
            from src.search.steel_searcher import SteelSearcher

            searchers["steel"] = SteelSearcher(cfg)

        # Semantic Scholar
        s2_cfg = {
            **cfg,
            "semantic_scholar_api_key": getattr(
                orchestrator.config, "semantic_scholar_api_key", None
            ),
        }
        semantic_scholar = SemanticScholarSearcher(s2_cfg)
        semantic_scholar.web_fallback = searchers.get("web")
        searchers["semantic_scholar"] = semantic_scholar

        # PubMed
        pubmed_cfg = {
            **cfg,
            "ncbi_api_key": getattr(orchestrator.config, "ncbi_api_key", None),
        }
        pubmed = PubMedSearcher(pubmed_cfg)
        pubmed.web_fallback = searchers.get("web")
        searchers["pubmed"] = pubmed

        # YouTube
        youtube_cfg = {
            **cfg,
            "youtube_api_key": getattr(orchestrator.config, "youtube_api_key", None),
        }
        youtube = YouTubeSearcher(youtube_cfg)
        youtube.web_fallback = searchers.get("web")
        searchers["youtube"] = youtube

        # Playwright
        if getattr(orchestrator.config, "playwright_enabled", False):
            from src.anti_blocking.residential_proxy import ResidentialProxyProvider
            from src.search.playwright_searcher import PlaywrightSearcher

            proxy_url = None
            if getattr(orchestrator.config, "residential_proxy_provider", None):
                try:
                    prov = ResidentialProxyProvider(
                        provider=orchestrator.config.residential_proxy_provider,
                        username=orchestrator.config.residential_proxy_username or "",
                        password=orchestrator.config.residential_proxy_password or "",
                    )
                    proxy_url = prov.get_proxy_url()
                except Exception as e:
                    logger.warning(f"Falha ao configurar proxy residencial: {e}")

            playwright_cfg = {
                **cfg,
                "proxy_url": proxy_url,
                "playwright_headless": getattr(
                    orchestrator.config, "playwright_headless", True
                ),
            }
            searchers["playwright"] = PlaywrightSearcher(playwright_cfg)

        # SerpAPI
        serpapi_key = getattr(orchestrator.config, "serpapi_api_key", None)
        serpapi_enabled = getattr(orchestrator.config, "serpapi_enabled", True)
        if serpapi_enabled and serpapi_key:
            from src.search.serpapi_searcher import SerpAPISearcher

            searchers["serpapi"] = SerpAPISearcher(api_key=serpapi_key)
            logger.info("SerpAPISearcher registrado como fallback de último recurso")

        return searchers
