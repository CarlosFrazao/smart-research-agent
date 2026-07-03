"""
Testes do Bloco 5 — Anti-Blocking Avançado.
Cobre: BrowserFingerprintGenerator, TLSFingerprintClient, CaptchaSolver,
ResidentialProxyProvider, PlaywrightSearcher, e a integração no cascateamento.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Config
from src.anti_blocking.browser_fingerprint import (
    BrowserFingerprintGenerator,
    BROWSER_PROFILES,
)
from src.anti_blocking.tls_fingerprint import TLSFingerprintClient
from src.anti_blocking.captcha_solver import CaptchaSolver
from src.anti_blocking.residential_proxy import ResidentialProxyProvider
from src.search.playwright_searcher import PlaywrightSearcher
from src.services.search_service import SearchService
from src.types import SearchResult


# ─────────────────────────────────────────────────────────────────────────────
# Testes do BrowserFingerprintGenerator
# ─────────────────────────────────────────────────────────────────────────────


class TestBrowserFingerprintGenerator:
    def test_generate_returns_valid_profile(self):
        profile = BrowserFingerprintGenerator.generate()
        assert isinstance(profile, dict)
        assert "user_agent" in profile
        assert "viewport" in profile
        assert "locale" in profile
        assert "timezone" in profile
        assert profile in BROWSER_PROFILES

    def test_random_user_agent_returns_string(self):
        ua = BrowserFingerprintGenerator.random_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 10

    def test_random_viewport_returns_dict(self):
        vp = BrowserFingerprintGenerator.random_viewport()
        assert isinstance(vp, dict)
        assert "width" in vp
        assert "height" in vp


# ─────────────────────────────────────────────────────────────────────────────
# Testes do TLSFingerprintClient
# ─────────────────────────────────────────────────────────────────────────────


class TestTLSFingerprintClient:
    @patch("curl_cffi.requests.Session")
    def test_init_available_if_curl_cffi_imported(self, mock_session):
        client = TLSFingerprintClient()
        assert client._available is True

    @patch("curl_cffi.requests.Session", side_effect=ImportError)
    def test_init_unavailable_if_import_error(self, mock_session):
        # Em ambientes sem curl_cffi
        with patch("logging.Logger.warning") as mock_warn:
            client = TLSFingerprintClient()
            assert client._available is False

    @pytest.mark.asyncio
    async def test_get_post_returns_none_if_unavailable(self):
        with patch("curl_cffi.requests.Session", side_effect=ImportError):
            client = TLSFingerprintClient()
            assert client._available is False
            res_get = await client.get("https://example.com")
            res_post = await client.post("https://example.com")
            assert res_get is None
            assert res_post is None

    @pytest.mark.asyncio
    async def test_get_success_mock(self):
        client = TLSFingerprintClient()
        client._available = True
        client._session = MagicMock()

        mock_response = MagicMock()
        mock_response.text = "<html>HTML Retornado</html>"
        client._session.get.return_value = mock_response

        res = await client.get("https://example.com")
        assert res == "<html>HTML Retornado</html>"
        client._session.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_success_mock(self):
        client = TLSFingerprintClient()
        client._available = True
        client._session = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        client._session.post.return_value = mock_response

        res = await client.post("https://example.com", json={"data": 123})
        assert res == {"success": True}
        client._session.post.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Testes do CaptchaSolver
# ─────────────────────────────────────────────────────────────────────────────


class TestCaptchaSolver:
    @pytest.mark.asyncio
    async def test_solve_returns_none_with_empty_key(self):
        solver = CaptchaSolver("2captcha", "")
        res = await solver.solve_recaptcha_v2("sitekey", "https://url.com")
        assert res is None

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_none(self):
        solver = CaptchaSolver("unsupported_provider", "key123")
        res = await solver.solve_recaptcha_v2("sitekey", "https://url.com")
        assert res is None

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_solve_2captcha_success(self, mock_post):
        solver = CaptchaSolver("2captcha", "fakekey")

        # Mock submissão inicial
        mock_resp_submit = MagicMock()
        mock_resp_submit.json.return_value = {"request": "task_id_999"}
        mock_post.return_value = mock_resp_submit

        # Mock polling do resultado
        mock_resp_poll = MagicMock()
        mock_resp_poll.json.return_value = {"status": 1, "request": "solved_token_abc"}

        with patch("httpx.AsyncClient.get", return_value=mock_resp_poll) as mock_get:
            with patch("asyncio.sleep", AsyncMock()):  # skip delay no teste
                res = await solver.solve_recaptcha_v2("sitekey", "https://url.com")
                assert res == "solved_token_abc"
                mock_get.assert_called_once()

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_solve_capsolver_success(self, mock_post):
        solver = CaptchaSolver("capsolver", "fakekey")

        # Mock submissão inicial e polling (ambos usam POST no capsolver)
        mock_resp_submit = MagicMock()
        mock_resp_submit.json.return_value = {"taskId": "task_id_888"}

        mock_resp_poll = MagicMock()
        mock_resp_poll.json.return_value = {
            "status": "ready",
            "solution": {"gRecaptchaResponse": "solved_token_xyz"},
        }

        mock_post.side_effect = [mock_resp_submit, mock_resp_poll]

        with patch("asyncio.sleep", AsyncMock()):
            res = await solver.solve_recaptcha_v2("sitekey", "https://url.com")
            assert res == "solved_token_xyz"


# ─────────────────────────────────────────────────────────────────────────────
# Testes do ResidentialProxyProvider
# ─────────────────────────────────────────────────────────────────────────────


class TestResidentialProxyProvider:
    def test_brightdata_url_format(self):
        prov = ResidentialProxyProvider("brightdata", "user123", "pass456")
        url = prov.get_proxy_url(country="br")
        assert "user123-country-br:pass456@brd.superproxy.io:33335" in url
        assert url.startswith("http://")

    def test_smartproxy_url_format(self):
        prov = ResidentialProxyProvider("smartproxy", "user123", "pass456")
        url = prov.get_proxy_url()
        assert "user123:pass456@gate.smartproxy.com:7000" in url
        assert url.startswith("http://")

    def test_invalid_provider_raises_error(self):
        with pytest.raises(ValueError):
            ResidentialProxyProvider("invalid", "u", "p")

    def test_get_httpx_proxies(self):
        prov = ResidentialProxyProvider("smartproxy", "u", "p")
        proxies = prov.get_httpx_proxies()
        assert "http://" in proxies
        assert "https://" in proxies
        assert proxies["http://"] == prov.get_proxy_url()


# ─────────────────────────────────────────────────────────────────────────────
# Testes do PlaywrightSearcher
# ─────────────────────────────────────────────────────────────────────────────


class TestPlaywrightSearcher:
    def test_init_defaults(self):
        cfg = {"timeout": 10, "max_results": 5, "enabled": True}
        searcher = PlaywrightSearcher(cfg)
        assert searcher._browser is None
        assert searcher._playwright is None
        assert searcher._proxy_url is None

    @pytest.mark.asyncio
    @patch("playwright.async_api.async_playwright")
    async def test_scrape_success(self, mock_playwright_launcher):
        # Configurar mocks encadeados do Playwright
        mock_p = MagicMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_playwright_launcher.return_value.start = AsyncMock(return_value=mock_p)
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_page.content = AsyncMock(return_value="<html>Hello Stealth</html>")

        cfg = {
            "timeout": 10,
            "max_results": 5,
            "enabled": True,
            "playwright_headless": True,
        }
        searcher = PlaywrightSearcher(cfg)

        with patch("playwright_stealth.Stealth") as mock_stealth_cls:
            mock_stealth_inst = MagicMock()
            mock_stealth_inst.apply_stealth_async = AsyncMock()
            mock_stealth_cls.return_value = mock_stealth_inst
            with patch("asyncio.sleep", AsyncMock()):
                html = await searcher.scrape("https://example.com")
                assert html == "<html>Hello Stealth</html>"

                # Deve instanciar lazy e salvar
                assert searcher._browser == mock_browser

                # Fechar
                await searcher.close()
                assert searcher._browser is None

    @pytest.mark.asyncio
    @patch("playwright.async_api.async_playwright")
    async def test_search_and_normalize(self, mock_playwright_launcher):
        mock_p = MagicMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_playwright_launcher.return_value.start = AsyncMock(return_value=mock_p)
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_page.content = AsyncMock(return_value="<html>Conteúdo de teste</html>")

        cfg = {"timeout": 10}
        searcher = PlaywrightSearcher(cfg)

        with patch("playwright_stealth.Stealth") as mock_stealth_cls:
            mock_stealth_inst = MagicMock()
            mock_stealth_inst.apply_stealth_async = AsyncMock()
            mock_stealth_cls.return_value = mock_stealth_inst
            with patch("asyncio.sleep", AsyncMock()):
                res = await searcher.search("https://example.com")
                assert len(res) == 1
                assert res[0].source == "playwright"
                assert "example.com" in res[0].url
                assert "Conteúdo de teste" in res[0].description

                norm = searcher.normalize(
                    {"title": "T", "url": "U", "description": "D"}
                )
                assert isinstance(norm, SearchResult)
                assert norm.title == "T"
                assert norm.url == "U"
                assert norm.description == "D"


# ─────────────────────────────────────────────────────────────────────────────
# Testes da Integração na Cascata de Scrapers (select_scraper_for_url)
# ─────────────────────────────────────────────────────────────────────────────


class TestAntiBlockingIntegration:
    @pytest.mark.asyncio
    async def test_cascade_tries_playwright_after_steel_before_jina(self):
        # Mocks para o Orchestrator
        mock_orch = MagicMock()

        # Config customizada
        config = Config()
        config.playwright_enabled = True
        config.spider_enabled = True
        config.steel_enabled = True
        mock_orch.config = config

        # Mocks para searchers
        mock_firecrawl = AsyncMock()
        mock_spider = AsyncMock()
        mock_steel = AsyncMock()
        mock_playwright = AsyncMock()

        # Todos os primeiros scrapers falham retornando nada ou lançando exceção
        mock_firecrawl.search.side_effect = Exception("Firecrawl block")
        mock_spider.search.return_value = []
        mock_steel.search.return_value = []

        # Playwright é bem-sucedido
        mock_playwright.search.return_value = [
            SearchResult(
                source="playwright",
                title="Playwright Success",
                url="https://site-protegido.com",
                description="Conteúdo completo extraído pelo browser stealth " * 20,
            )
        ]

        mock_orch.searchers = {
            "firecrawl": mock_firecrawl,
            "spider": mock_spider,
            "steel": mock_steel,
            "playwright": mock_playwright,
        }

        search_service = SearchService(mock_orch)
        res = await search_service.select_scraper_for_url("https://site-protegido.com")

        # Verifica se obteve o retorno do Playwright
        assert len(res) == 1
        assert res[0].source == "playwright"
        assert "Playwright Success" in res[0].title

        # Verifica ordem de chamadas
        mock_firecrawl.search.assert_called_once()
        mock_spider.search.assert_called_once()
        mock_steel.search.assert_called_once()
        mock_playwright.search.assert_called_once()
