"""Fábrica de instanciação dinâmica de searchers do Smart Research Agent.

Centraliza a criacao e configuracao de todos os searchers disponíveis,
permitindo que o orquestrador seja inicializado sem imports acoplados.
"""

import logging
import os
from typing import Any

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
from src.search.jina_searcher import JinaSearcher
from src.search.firecrawl_searcher import FirecrawlSearcher
from src.search.spider_searcher import SpiderSearcher
from src.search.steel_searcher import SteelSearcher
from src.search.serpapi_searcher import SerpAPISearcher

# Importações dos searchers órfãos (FASE 0.1)
from src.search.multilingual_searcher import MultilingualSearcher
from src.search.scraping_searcher import ScrapingSearcher

try:
    from src.search.producthunt_searcher import ProductHuntSearcher
except ImportError:
    ProductHuntSearcher = None

try:
    from src.anti_blocking.residential_proxy import ResidentialProxyProvider
    from src.search.playwright_searcher import PlaywrightSearcher
except ImportError:
    ResidentialProxyProvider = None
    PlaywrightSearcher = None

# Importações dos conectores Enterprise (Notion / Confluence / SharePoint)
try:
    from src.connectors import (
        ConfluenceClient,
        NotionClient,
        SharePointClient,
    )
except ImportError:
    NotionClient = None
    ConfluenceClient = None
    SharePointClient = None

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
        if ProductHuntSearcher is not None:
            searchers["producthunt"] = ProductHuntSearcher(cfg)

        # Firecrawl / Jina
        if getattr(orchestrator.config, "host_mode", False):
            logger.info(
                "HOST MODE ativo — Firecrawl substituido por JinaSearcher como fallback"
            )
            jina_cfg = {
                **cfg,
                "jina_base_url": getattr(
                    orchestrator.config, "jina_reader_base_url", "https://r.jina.ai/"
                ),
            }
            searchers["firecrawl"] = JinaSearcher(jina_cfg)
        else:
            searchers["firecrawl"] = FirecrawlSearcher(cfg)

        # Spider
        if orchestrator.config.spider_enabled:
            searchers["spider"] = SpiderSearcher(cfg)

        # Steel
        if orchestrator.config.steel_enabled:
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
        if (
            getattr(orchestrator.config, "playwright_enabled", False)
            and PlaywrightSearcher is not None
        ):
            proxy_url = None
            if (
                getattr(orchestrator.config, "residential_proxy_provider", None)
                and ResidentialProxyProvider is not None
            ):
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
            searchers["serpapi"] = SerpAPISearcher(api_key=serpapi_key)
            logger.info("SerpAPISearcher registrado como fallback de último recurso")

        # ── Conectores Enterprise (Notion / Confluence / SharePoint) ──
        # Fontes primárias/secundárias em 5 de 7 domínios, mas historicamente
        # NUNCA registradas no factory → silenciosamente ignoradas. O registro
        # só ocorre quando as respectivas credenciais estão presentes.
        if NotionClient is not None and getattr(
            orchestrator.config, "notion_api_key", None
        ):
            searchers["notion"] = NotionClient(
                api_key=orchestrator.config.notion_api_key
            )
            logger.info("NotionClient registrado (conector Enterprise)")

        if (
            ConfluenceClient is not None
            and getattr(orchestrator.config, "confluence_api_key", None)
            and getattr(orchestrator.config, "confluence_base_url", None)
            and getattr(orchestrator.config, "confluence_username", None)
        ):
            searchers["confluence"] = ConfluenceClient(
                api_token=getattr(orchestrator.config, "confluence_api_key", None),
                base_url=getattr(orchestrator.config, "confluence_base_url", None),
                username=getattr(orchestrator.config, "confluence_username", None),
            )
            logger.info("ConfluenceClient registrado (conector Enterprise)")

        if (
            SharePointClient is not None
            and getattr(orchestrator.config, "sharepoint_client_id", None)
            and getattr(orchestrator.config, "sharepoint_client_secret", None)
            and getattr(orchestrator.config, "sharepoint_tenant_id", None)
        ):
            searchers["sharepoint"] = SharePointClient(
                client_id=getattr(orchestrator.config, "sharepoint_client_id", None),
                client_secret=getattr(
                    orchestrator.config, "sharepoint_client_secret", None
                ),
                tenant_id=getattr(orchestrator.config, "sharepoint_tenant_id", None),
            )
            logger.info("SharePointClient registrado (conector Enterprise)")

        # ── FASE 0.1: Registrar searchers órfãos ──
        # MultilingualSearcher — wrapper sobre SearXNG/Web com tradução LLM
        if os.getenv("SRA_MULTILINGUAL_ENABLED", "false").lower() == "true":
            ml_base = searchers.get("searxng") or searchers.get("web")
            ml_llm = getattr(orchestrator, "llm", None)
            if ml_base and ml_llm:
                searchers["multilingual"] = MultilingualSearcher(
                    base_searcher=ml_base,
                    llm_client=ml_llm,
                    concurrency=3,
                )
                logger.info(
                    "MultilingualSearcher registrado sobre %s",
                    ml_base.__class__.__name__,
                )

        # ScrapingSearcher — cascata resiliente Firecrawl→Spider→Steel→Jina
        if os.getenv("SRA_SCRAPING_ENABLED", "false").lower() == "true":
            scraping_cfg = {
                **cfg,
                "firecrawl_api_key": orchestrator.config.firecrawl_api_key,
                "firecrawl_base_url": orchestrator.config.firecrawl_base_url,
                "spider_api_key": orchestrator.config.spider_api_key,
                "steel_api_key": orchestrator.config.steel_api_key,
                "jina_base_url": getattr(
                    orchestrator.config, "jina_reader_base_url", "https://r.jina.ai/"
                ),
            }
            searchers["scraping"] = ScrapingSearcher(scraping_cfg)
            logger.info(
                "ScrapingSearcher registrado (cascata Firecrawl→Spider→Steel→Jina)"
            )

        # ── FASE 1.2: Auto-Discovery no SearcherFactory ──
        # Registrar searchers decorados com @register_searcher
        import importlib
        import pkgutil
        import src.search as _search_pkg

        # Importar todos os módulos de src/search/ para garantir que os decorators rodem
        for _importer, _modname, _ispkg in pkgutil.iter_modules(_search_pkg.__path__):
            if _modname not in ("factory", "registry", "base_searcher"):
                try:
                    importlib.import_module(f"src.search.{_modname}")
                except Exception as _e:
                    logger.debug(
                        "Auto-import de src.search.%s falhou: %s", _modname, _e
                    )

        from src.search.registry import get_registry

        for _name, _meta in get_registry().items():
            if _name in searchers:
                continue  # precedência do registro manual
            if _meta.get("enabled_env"):
                if os.getenv(_meta["enabled_env"], "false").lower() != "true":
                    continue
            if _meta.get("requires_key"):
                if not os.getenv(_meta["requires_key"]):
                    logger.debug(
                        "Searcher '%s' pulado: %s não configurada",
                        _name,
                        _meta["requires_key"],
                    )
                    continue
            try:
                searchers[_name] = _meta["cls"](cfg)
                logger.info(
                    "Searcher '%s' auto-registrado via @register_searcher", _name
                )
            except Exception as _e:
                logger.warning("Falha ao auto-registrar '%s': %s", _name, _e)

        # ── FASE 6: Registrar fontes genéricas do catálogo YAML ──
        # GenericAPISearcher transforma cada entrada de config/generic_sources.yaml
        # em uma fonte de busca sem escrever código Python novo. Searchers
        # dedicados têm precedência (não são sobrescritos).
        try:
            from src.search.generic_api_searcher import (
                GenericAPISearcher,
                list_generic_source_ids,
            )

            for _gid in list_generic_source_ids():
                if _gid in searchers:
                    continue  # não sobrescrever um searcher dedicado
                try:
                    searchers[_gid] = GenericAPISearcher(_gid, cfg)
                    logger.info(
                        "Fonte genérica '%s' registrada via GenericAPISearcher", _gid
                    )
                except Exception as _e:
                    logger.warning(
                        "Falha ao registrar fonte genérica '%s': %s", _gid, _e
                    )
        except Exception as _e:
            logger.warning("Não foi possível carregar generic_sources.yaml: %s", _e)

        return searchers

    @classmethod
    def get_available_searchers(cls) -> set[str]:
        """Retorna o conjunto de nomes de searchers reconhecidos pelo factory.

        Nao instancia nenhum searcher (evita exigir API keys/network no boot),
        apenas lista os nomes validos para validacao de roteamento (ex: usado
        pelo Universal Router do ``SourcePlanner`` para descartar nomes
        inexistentes sugeridos pelo LLM).

        Returns:
            set[str]: Nomes de searchers registrados (manual + @register_searcher).
        """
        # Searchers registrados manualmente em create_searchers()
        known = {
            "github",
            "reddit",
            "hackernews",
            "awesome",
            "arxiv",
            "web",
            "rss",
            "searxng",
            "stackoverflow",
            "wayback",
            "producthunt",
            "firecrawl",
            "jina",
            "spider",
            "steel",
            "semantic_scholar",
            "pubmed",
            "youtube",
            "playwright",
            "serpapi",
            "multilingual",
            "scraping",
        }
        # Searchers auto-descobertos via @register_searcher
        try:
            from src.search.registry import get_registry

            known.update(get_registry().keys())
        except Exception:
            pass
        # Fontes genéricas do catálogo YAML (FASE 6)
        try:
            from src.search.generic_api_searcher import list_generic_source_ids

            known.update(list_generic_source_ids())
        except Exception:
            pass
        return known
