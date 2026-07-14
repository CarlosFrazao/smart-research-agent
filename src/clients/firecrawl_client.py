import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [1.0, 2.0, 4.0]


class FirecrawlClient:
    def __init__(
        self, api_key: str, base_url: str | None = None, config: Any | None = None
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.config = config
        self.firecrawl_redact_pii = (
            getattr(config, "firecrawl_redact_pii", False) if config else False
        )
        self.firecrawl_lockdown_mode = (
            getattr(config, "firecrawl_lockdown_mode", False) if config else False
        )
        self.firecrawl_deterministic_json = (
            getattr(config, "firecrawl_deterministic_json", False) if config else False
        )
        self.firecrawl_research_index_enabled = (
            getattr(config, "firecrawl_research_index_enabled", True)
            if config
            else True
        )
        self.app = None
        try:
            from firecrawl import V1FirecrawlApp

            self.app = (
                V1FirecrawlApp(api_key=api_key, api_url=base_url)
                if base_url
                else V1FirecrawlApp(api_key=api_key)
            )
            logger.info("Firecrawl SDK v4 (V1FirecrawlApp) inicializado com sucesso.")
        except Exception as e:
            logger.warning(
                f"Erro ao inicializar Firecrawl SDK: {e}. Usando fallback HTTP."
            )

        # Inicializa o ScrapingRaceClient de forma preguiçosa
        self._race_client = None

        # Flag de auth: quando True, o token é inválido/expirado (ex.: 401).
        # Evita retries infinitos e sinaliza ao searcher pai que deve cascatear
        # para o web_fallback (ver GAP 1 do PLANO_FECHAR_GAPS.md).
        self.auth_failed: bool = False

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """Detecta erro de autenticação (token inválido/expirado) em uma exceção.

        Reconhece 401, mensagens de unauthorized, api key inválida e
        autenticação genérica — cobrindo o caso real do stress test
        (``401 Unauthorized`` com FIRECRAWL_API_KEY configurado).
        """
        msg = str(exc).lower()
        return any(
            k in msg
            for k in (
                "401",
                "unauthorized",
                "unauthenticated",
                "invalid api key",
                "api key",
                "authentication",
                "forbidden",
                "403",
            )
        )

    @property
    def race_client(self):
        if self._race_client is None:
            from src.clients.scraping_race_client import ScrapingRaceClient

            self._race_client = ScrapingRaceClient(self)
        return self._race_client

    def _is_retryable(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(
            k in msg
            for k in ("429", "rate limit", "timeout", "503", "502", "connection")
        )

    async def _with_retry(self, coro_fn, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable(exc) or attempt == len(_RETRY_DELAYS):
                    raise
                logger.warning(
                    f"Firecrawl tentativa {attempt} falhou ({exc}), aguardando {delay}s..."
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _normalize_search_results(self, results) -> list[dict[str, Any]]:
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
                    "markdown": getattr(item, "markdown", "")
                    or getattr(item, "description", "")
                    or "",
                    "description": getattr(item, "description", "") or "",
                }
                for item in items
            ]
        if isinstance(results, list):
            return results
        if isinstance(results, dict):
            return results.get("data", [])
        return []

    def _normalize_scrape_result(self, result) -> dict[str, Any]:
        """Normaliza resposta de scrape do SDK v4 para dict."""
        if result is None:
            return {}
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if isinstance(result, dict):
            return result.get("data", result)
        return {}

    async def search(
        self, query: str, limit: int = 10, stealth: bool = True
    ) -> list[dict[str, Any]]:
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
            if self._is_auth_error(e):
                self.auth_failed = True
                logger.error(
                    f"Firecrawl: token inválido/expirado (401/unauthorized). "
                    f"Desativando e sinalizando fallback. Erro: {e}"
                )
                return []
            logger.warning(
                f"Busca Firecrawl com parâmetros estendidos falhou ({e}). Tentando busca simples..."
            )
            try:
                results = await self._with_retry(
                    asyncio.to_thread,
                    self.app.search,
                    query,
                    limit=limit,
                )
                return self._normalize_search_results(results)
            except Exception as e2:
                if self._is_auth_error(e2):
                    self.auth_failed = True
                    logger.error(
                        f"Firecrawl: token inválido/expirado (401/unauthorized). "
                        f"Desativando e sinalizando fallback. Erro: {e2}"
                    )
                logger.error(
                    f"Firecrawl search erro (todos os retries esgotados): {e2}"
                )
                return []

    async def search_simplified(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Tenta busca com query simplificada (primeiras 3 palavras) como bypass de bloqueio."""
        simplified = " ".join(query.split()[:3])
        if simplified == query:
            return []
        logger.info(f"Tentando query simplificada: '{simplified}'")
        return await self.search(simplified, limit=limit)

    async def scrape(
        self, url: str, formats: list[str] | None = None, stealth: bool = True
    ) -> dict[str, Any]:
        """Realiza a raspagem concorrente (Scraping Race) para máxima velocidade e taxa de sucesso."""
        return await self.race_client.scrape(url, formats=formats)

    async def _direct_scrape_call(
        self, url: str, formats: list[str] | None = None
    ) -> dict[str, Any]:
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
            logger.warning(
                f"Firecrawl scrape avançado falhou para {url} ({e}). Tentando scrape simples..."
            )
            try:
                result = await self._with_retry(
                    asyncio.to_thread,
                    self.app.scrape_url,
                    url,
                    formats=formats,
                )
                return self._normalize_scrape_result(result)
            except Exception as e2:
                logger.error(
                    f"Firecrawl scrape erro em {url} (todos os retries esgotados): {e2}"
                )
                return {}

    async def search_research_index(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
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

    async def crawl(self, url: str, limit: int = 50) -> list[dict[str, Any]]:
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

    async def map_urls(self, url: str) -> list[str]:
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

    # ------------------------------------------------------------------
    # Agent Mode (Plano SRA v6.0 → item 3.5)
    # ------------------------------------------------------------------

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Chamada HTTP POST genérica à API Firecrawl. Retorna {} se API key ausente."""
        if not self.api_key:
            return {}
        import aiohttp

        base = self.base_url or "https://api.firecrawl.dev"
        url = f"{base}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.warning(
                            f"Firecrawl POST {endpoint} → {resp.status}: {text[:200]}"
                        )
                        return {}
                    return await resp.json()
        except Exception as exc:
            logger.warning(f"Firecrawl._post {endpoint} falhou: {exc}")
            return {}

    async def agent_search(
        self,
        task: str,
        max_steps: int = 10,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """
        Firecrawl Agent Mode — pesquisa baseada em tarefa com raciocínio multi-step.
        Retorna dict com 'answer', 'sources', 'steps'. Retorna {} sem crash se API key ausente.
        """
        if not self.api_key:
            return {}
        # Tenta usar SDK v4 se disponível (app.agent_search)
        if self.app and hasattr(self.app, "agent_search"):
            try:
                result = await asyncio.to_thread(
                    self.app.agent_search,
                    task,
                    max_steps=max_steps,
                    timeout=timeout * 1000,
                )
                if hasattr(result, "model_dump"):
                    return result.model_dump()
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                logger.warning(
                    f"FirecrawlClient.agent_search SDK falhou ({exc}). Tentando REST..."
                )
        # Fallback REST
        payload = {"prompt": task, "maxSteps": max_steps, "timeout": timeout * 1000}
        return await self._post("/v1/agent", payload)

    async def interact(
        self,
        url: str,
        instructions: list[str],
        wait_ms: int = 1000,
    ) -> dict[str, Any]:
        """
        Firecrawl Interact — executa sequência de instruções de browser num site.
        Retorna dict com 'result', 'screenshots'. Retorna {} sem crash se API key ausente.
        """
        if not self.api_key:
            return {}
        if self.app and hasattr(self.app, "interact"):
            try:
                result = await asyncio.to_thread(
                    self.app.interact,
                    url,
                    instructions=instructions,
                    wait=wait_ms,
                )
                if hasattr(result, "model_dump"):
                    return result.model_dump()
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                logger.warning(
                    f"FirecrawlClient.interact SDK falhou ({exc}). Tentando REST..."
                )
        payload = {"url": url, "instructions": instructions, "wait": wait_ms}
        return await self._post("/v1/interact", payload)

    async def map_domain(self, url: str, limit: int = 1000) -> list[str]:
        """
        Firecrawl map completo de domínio — retorna lista de URLs encontradas.
        Retorna [] sem crash se API key ausente.
        """
        if not self.api_key:
            return []
        if self.app and hasattr(self.app, "map_url"):
            try:
                result = await self._with_retry(
                    asyncio.to_thread,
                    self.app.map_url,
                    url,
                    limit=limit,
                )
                if hasattr(result, "links"):
                    return result.links or []
                if hasattr(result, "model_dump"):
                    return result.model_dump().get("links", [])
                if isinstance(result, dict):
                    return result.get("links", [])
                return []
            except Exception as exc:
                logger.warning(
                    f"FirecrawlClient.map_domain falhou ({exc}). Tentando REST..."
                )
        data = await self._post("/v1/map", {"url": url, "limit": limit})
        return data.get("links", [])

    async def batch_scrape(
        self,
        urls: list[str],
        formats: list[str] | None = None,
        concurrency: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Scraping paralelo de múltiplas URLs. Retorna lista de dicts com resultado de cada URL.
        Retorna [] sem crash se API key ausente.
        """
        if not self.api_key or not urls:
            return []
        formats = formats or ["markdown"]
        # Tenta batch endpoint REST
        payload = {"urls": urls, "formats": formats}
        data = await self._post("/v1/batch/scrape", payload)
        if data:
            items = data.get("data", data.get("results", []))
            if isinstance(items, list):
                return items
        # Fallback: scrape individual com semáforo
        import asyncio as _aio

        sem = _aio.Semaphore(concurrency)

        async def _one(url: str) -> dict[str, Any]:
            async with sem:
                try:
                    return await self._direct_scrape_call(url, formats=formats)
                except Exception:
                    return {}

        results = await _aio.gather(*[_one(u) for u in urls], return_exceptions=False)
        return list(results)
