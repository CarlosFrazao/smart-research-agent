from typing import Optional, Dict, Any, List
import asyncio
import logging

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [1.0, 2.0, 4.0]


class FirecrawlClient:
    def __init__(self, api_key: str, base_url: Optional[str] = None, config: Optional[Any] = None):
        self.api_key = api_key
        self.base_url = base_url
        self.config = config
        self.firecrawl_redact_pii = getattr(config, "firecrawl_redact_pii", False) if config else False
        self.firecrawl_lockdown_mode = getattr(config, "firecrawl_lockdown_mode", False) if config else False
        self.firecrawl_deterministic_json = getattr(config, "firecrawl_deterministic_json", False) if config else False
        self.firecrawl_research_index_enabled = getattr(config, "firecrawl_research_index_enabled", True) if config else True
        self.app = None
        try:
            from firecrawl import V1FirecrawlApp
            self.app = V1FirecrawlApp(api_key=api_key, api_url=base_url) if base_url else V1FirecrawlApp(api_key=api_key)
            logger.info("Firecrawl SDK v4 (V1FirecrawlApp) inicializado com sucesso.")
        except Exception as e:
            logger.warning(f"Erro ao inicializar Firecrawl SDK: {e}. Usando fallback HTTP.")
        
        # Inicializa o ScrapingRaceClient apontando para nós mesmos
        from src.clients.scraping_race_client import ScrapingRaceClient
        self.race_client = ScrapingRaceClient(self)

    def _is_retryable(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in ("429", "rate limit", "timeout", "503", "502", "connection"))

    async def _with_retry(self, coro_fn, *args, **kwargs):
        last_exc: Optional[Exception] = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable(exc) or attempt == len(_RETRY_DELAYS):
                    raise
                logger.warning(f"Firecrawl tentativa {attempt} falhou ({exc}), aguardando {delay}s...")
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _normalize_search_results(self, results) -> List[Dict[str, Any]]:
        """Normaliza resposta do SDK v4 para lista de dicts."""
        if results is None:
            return []
        # V1SearchResponse tem .data como lista de V1SearchResult
        if hasattr(results, "data") and results.data is not None:
            items = results.data
            return [
                {
                    "title": getattr(item, "title", "") or "",
                    "url": getattr(item, "url", "") or "",
                    "markdown": getattr(item, "markdown", "") or getattr(item, "description", "") or "",
                    "description": getattr(item, "description", "") or "",
                }
                for item in items
            ]
        if isinstance(results, list):
            return results
        if isinstance(results, dict):
            return results.get("data", [])
        return []

    def _normalize_scrape_result(self, result) -> Dict[str, Any]:
        """Normaliza resposta de scrape do SDK v4 para dict."""
        if result is None:
            return {}
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if isinstance(result, dict):
            return result.get("data", result)
        return {}

    async def search(self, query: str, limit: int = 10, stealth: bool = True) -> List[Dict[str, Any]]:
        if not self.app:
            return []
        try:
            from firecrawl.v1.client import V1ScrapeOptions
            scrape_options = V1ScrapeOptions(
                formats=["markdown"],
                skipTlsVerification=True,
                timeout=30000,
            )
            params = {
                "limit": limit,
                "scrape_options": scrape_options,
                "timeout": 30000,
            }
            if self.firecrawl_deterministic_json:
                params["deterministic_json"] = True

            results = await self._with_retry(
                asyncio.to_thread,
                self.app.search,
                query,
                **params,
            )
            return self._normalize_search_results(results)
        except Exception as e:
            logger.warning(f"Busca Firecrawl com parâmetros estendidos falhou ({e}). Tentando busca simples...")
            try:
                results = await self._with_retry(
                    asyncio.to_thread,
                    self.app.search,
                    query,
                    limit=limit,
                )
                return self._normalize_search_results(results)
            except Exception as e2:
                logger.error(f"Firecrawl search erro (todos os retries esgotados): {e2}")
                return []

    async def search_simplified(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Tenta busca com query simplificada (primeiras 3 palavras) como bypass de bloqueio."""
        simplified = " ".join(query.split()[:3])
        if simplified == query:
            return []
        logger.info(f"Tentando query simplificada: '{simplified}'")
        return await self.search(simplified, limit=limit)

    async def scrape(self, url: str, formats: Optional[List[str]] = None, stealth: bool = True) -> Dict[str, Any]:
        """Realiza a raspagem concorrente (Scraping Race) para máxima velocidade e taxa de sucesso."""
        return await self.race_client.scrape(url, formats=formats)

    async def _direct_scrape_call(self, url: str, formats: Optional[List[str]] = None) -> Dict[str, Any]:
        """Chamada de scraping direta à API local/remota do Firecrawl sem concorrência da corrida."""
        if not self.app:
            return {}
        formats = formats or ["markdown"]
        try:
            params = {
                "formats": formats,
                "skip_tls_verification": True,
                "wait_for": 3000,
                "timeout": 45000,
            }
            if self.firecrawl_redact_pii:
                params["redact_pii"] = True
            if self.firecrawl_lockdown_mode:
                params["lockdown_mode"] = True

            result = await self._with_retry(
                asyncio.to_thread,
                self.app.scrape_url,
                url,
                **params,
            )
            return self._normalize_scrape_result(result)
        except Exception as e:
            logger.warning(f"Firecrawl scrape avançado falhou para {url} ({e}). Tentando scrape simples...")
            try:
                result = await self._with_retry(
                    asyncio.to_thread,
                    self.app.scrape_url,
                    url,
                    formats=formats,
                )
                return self._normalize_scrape_result(result)
            except Exception as e2:
                logger.error(f"Firecrawl scrape erro em {url} (todos os retries esgotados): {e2}")
                return {}

    async def search_research_index(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Busca no Firecrawl Research Index (3M+ papers arXiv + GitHub code)."""
        if not self.app:
            return []
        try:
            results = await self._with_retry(
                asyncio.to_thread,
                self.app.search,
                query,
                limit=limit,
                index="research",  # Novo parâmetro do SDK 4.30.3
            )
            return self._normalize_search_results(results)
        except Exception as e:
            logger.warning(f"Firecrawl Research Index falhou ({e}).")
            return []

    async def crawl(self, url: str, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.app:
            return []
        try:
            result = await self._with_retry(
                asyncio.to_thread, self.app.crawl_url, url, limit=limit
            )
            if hasattr(result, "model_dump"):
                return result.model_dump().get("data", [])
            if isinstance(result, dict):
                return result.get("data", [])
            return []
        except Exception as e:
            logger.error(f"Firecrawl crawl erro em {url}: {e}")
            return []

    async def map_urls(self, url: str) -> List[str]:
        if not self.app:
            return []
        try:
            result = await self._with_retry(asyncio.to_thread, self.app.map_url, url)
            if hasattr(result, "links"):
                return result.links or []
            if hasattr(result, "model_dump"):
                return result.model_dump().get("links", [])
            if isinstance(result, dict):
                return result.get("links", [])
            return []
        except Exception as e:
            logger.error(f"Firecrawl map erro em {url}: {e}")
            return []
