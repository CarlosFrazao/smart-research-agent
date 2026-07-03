"""
Playwright Stealth — Último recurso para sites JS-heavy ou protegidos por Cloudflare/DataDome.
Simula comportamento humano completo com fingerprint camuflado.

Custo: ~500ms-3s por página. Use apenas como fallback de último recurso.
O browser é inicializado apenas na primeira chamada (lazy init) e reutilizado entre requests.
"""
import asyncio
import random
import logging
from typing import Any, Dict, List, Optional

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class PlaywrightSearcher(BaseSearcher):
    """
    Scraper que usa browser real Chromium com stealth para evitar detecção.

    Estratégia de evasão:
    - Fingerprint completo de browser (UA, viewport, locale, timezone) sortado aleatoriamente
    - playwright-stealth remove sinais internos de automação (navigator.webdriver, etc.)
    - Delays e scroll semi-humanos para enganar análise comportamental
    - Suporte opcional a proxy residencial rotativo
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._browser = None
        self._playwright = None
        self._proxy_url: Optional[str] = config.get("proxy_url")
        self._headless: bool = config.get("playwright_headless", True)

    async def _get_browser(self):
        """Inicia o browser apenas na primeira chamada (lazy init)."""
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--disable-default-apps",
                ]
            )
        return self._browser

    async def scrape(self, url: str, wait_for: str = "networkidle") -> Optional[str]:
        """
        Extrai o HTML completo de uma URL com anti-detecção stealth.

        Fluxo:
          1. Gera fingerprint de browser aleatório
          2. Abre contexto isolado com fingerprint + proxy opcional
          3. Aplica playwright-stealth ao page object
          4. Navega com delays semi-humanos (0.3–1.2s) e scroll gradual
          5. Retorna HTML via page.content()
          6. Sempre fecha page e context no finally (sem leaks)
        """
        from src.anti_blocking.browser_fingerprint import BrowserFingerprintGenerator

        browser = await self._get_browser()
        profile = BrowserFingerprintGenerator.generate()

        context_options = {
            "user_agent": profile["user_agent"],
            "viewport": profile["viewport"],
            "locale": profile["locale"],
            "timezone_id": profile["timezone"],
        }

        # Injeta proxy residencial se configurado
        if self._proxy_url:
            context_options["proxy"] = {"server": self._proxy_url}

        context = await browser.new_context(**context_options)
        page = None
        try:
            from playwright_stealth import Stealth
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)

            # Delay de abertura semi-humano
            await asyncio.sleep(random.uniform(0.3, 1.2))
            await page.goto(url, wait_until=wait_for, timeout=30000)

            # Scroll gradual semi-humano (1–3 vezes)
            for _ in range(random.randint(1, 3)):
                await page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(0.2, 0.8))

            content = await page.content()
            return content

        except Exception as e:
            logger.warning(f"PlaywrightSearcher erro em '{url[:50]}': {e}")
            return None
        finally:
            if page:
                await page.close()
            await context.close()

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """
        Implementação do contrato BaseSearcher.
        'query' é tratado como URL direta para scraping via Playwright.
        Retorna lista com 1 SearchResult ou [] em caso de falha.
        """
        url = query
        html = await self.scrape(url)
        if not html:
            return []

        return [SearchResult(
            source="playwright",
            title=f"Playwright: {url}",
            url=url,
            description=html[:5000],   # captura os primeiros 5KB de conteúdo
            metrics={},
            raw={"html": html},
        )]

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normalização básica para compatibilidade com BaseSearcher."""
        return SearchResult(
            source="playwright",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics={},
            raw=raw_result,
        )

    async def close(self) -> None:
        """Encerra o browser e playwright runner. Deve ser chamado no shutdown do Orchestrator."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"PlaywrightSearcher: erro ao fechar browser: {e}")
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"PlaywrightSearcher: erro ao parar playwright: {e}")
            self._playwright = None
