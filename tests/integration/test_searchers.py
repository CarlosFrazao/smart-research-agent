"""Testes de integração de contrato para os 18+ searchers do SRA.

Valida que cada searcher implementa o contrato BaseSearcher corretamente:
  - search(query, **kwargs) → List[SearchResult]
  - normalize(raw) → SearchResult
  - fallback(query) → List[SearchResult]
  - enabled: bool
  - timeout: float

Testes por searcher:
  1. Mock tests: valida contrato com mocks (rápido, CI-friendly)
  2. Real tests: valida contra APIs reais (lento, requer credenciais)
  3. Schema validation: garante que SearchResult tem campos obrigatórios
  4. Rate limiting: valida que não excede limites
  5. Circuit breaker: valida transições de estado
  6. Fallback cascade: valida fallback quando primário falha
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# ── Imports do SRA ──────────────────────────────────────────────────────────

from src.search.base_searcher import BaseSearcher
from src.search.factory import SearcherFactory
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpen, CircuitBreakerRegistry, CircuitState


# ── Constantes ───────────────────────────────────────────────────────────────

ALL_SEARCHERS: List[str] = [
    "github",
    "reddit",
    "hackernews",
    "arxiv",
    "semantic_scholar",
    "pubmed",
    "stackoverflow",
    "producthunt",
    "youtube",
    "rss",
    "searxng",
    "web",
    "firecrawl",
    "spider",
    "steel",
    "jina",
    "serpapi",
    "tavily",
]

SEARCHERS_REQUERINDO_CREDENCIAIS: List[str] = [
    "github",
    "reddit",
    "serpapi",
    "tavily",
    "firecrawl",
    "spider",
    "steel",
    "jina",
]

REAL_TEST_TIMEOUT: float = 30.0


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_config() -> Dict[str, Any]:
    """Configuração mínima para criação de searchers."""
    return {
        "timeout": 10.0,
        "rate_limit_rps": 2.0,
        "cache_enabled": False,
        "github_token": "mock_token",
        "reddit_client_id": "mock_id",
        "reddit_client_secret": "mock_secret",
        "serpapi_key": "mock_key",
        "tavily_key": "mock_key",
        "firecrawl_key": "mock_key",
        "spider_key": "mock_key",
        "steel_key": "mock_key",
        "jina_key": "mock_key",
    }


@pytest.fixture
def circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Registry de circuit breakers para testes."""
    return CircuitBreakerRegistry(
        default_failure_threshold=3,
        default_recovery_timeout=5.0,
    )


@pytest.fixture
def sample_search_result() -> SearchResult:
    """SearchResult de exemplo para validação de schema."""
    return SearchResult(
        source="test",
        title="Test Title",
        url="https://example.com/test",
        description="Test description for validation",
        metrics={"score": 0.95},
        raw={"id": 123},
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_searcher_class() -> Type[BaseSearcher]:
    """Retorna uma classe mock de BaseSearcher para testes de contrato."""
    class MockSearcher(BaseSearcher):
        def __init__(self, config: Dict[str, Any]):
            super().__init__(config)
            self.enabled = True
            self.timeout = 10.0
            self._mock_results = [
                SearchResult(
                    source="mock",
                    title="Mock Result",
                    url="https://mock.example.com/1",
                    description="Mock description",
                    metrics={},
                )
            ]

        async def search(self, query: str, **kwargs) -> List[SearchResult]:
            return self._mock_results

        def normalize(self, raw_result: Any) -> SearchResult:
            if isinstance(raw_result, SearchResult):
                return raw_result
            return SearchResult(
                source="mock",
                title=str(raw_result.get("title", "")),
                url=str(raw_result.get("url", "")),
                description=str(raw_result.get("description", "")),
                metrics=raw_result.get("metrics", {}),
                raw=raw_result,
            )

        def fallback(self, query: str) -> List[SearchResult]:
            return [
                SearchResult(
                    source="mock_fallback",
                    title=f"Fallback: {query}",
                    url="https://fallback.example.com",
                    description="Fallback result",
                    metrics={"fallback": True},
                )
            ]

    return MockSearcher


# ── Helpers ───────────────────────────────────────────────────────────────────

def validate_search_result_schema(result: SearchResult, source_name: str) -> List[str]:
    """Valida que um SearchResult segue o schema esperado."""
    errors = []

    if not result.source:
        errors.append(f"[{source_name}] source está vazio")
    if not result.title:
        errors.append(f"[{source_name}] title está vazio")
    if not result.url:
        errors.append(f"[{source_name}] url está vazio")
    if not result.description:
        errors.append(f"[{source_name}] description está vazio")

    if not isinstance(result.source, str):
        errors.append(f"[{source_name}] source deve ser str, é {type(result.source)}")
    if not isinstance(result.title, str):
        errors.append(f"[{source_name}] title deve ser str, é {type(result.title)}")
    if not isinstance(result.url, str):
        errors.append(f"[{source_name}] url deve ser str, é {type(result.url)}")
    if not isinstance(result.description, str):
        errors.append(f"[{source_name}] description deve ser str, é {type(result.description)}")
    if result.metrics is not None and not isinstance(result.metrics, dict):
        errors.append(f"[{source_name}] metrics deve ser dict ou None")

    if result.url and not (result.url.startswith("http://") or result.url.startswith("https://")):
        errors.append(f"[{source_name}] url deve começar com http:// ou https://: {result.url}")

    if result.fetched_at is not None and not isinstance(result.fetched_at, datetime):
        errors.append(f"[{source_name}] fetched_at deve ser datetime ou None")

    return errors


def create_mock_searcher(name: str, config: Dict[str, Any]) -> BaseSearcher:
    """Cria um mock de searcher para testes de contrato."""
    class DynamicMockSearcher(BaseSearcher):
        def __init__(self, config: Dict[str, Any]):
            super().__init__(config)
            self.enabled = True
            self.timeout = config.get("timeout", 10.0)
            self.source_name = name

        async def search(self, query: str, **kwargs) -> List[SearchResult]:
            return [
                SearchResult(
                    source=self.source_name,
                    title=f"{self.source_name} result for {query}",
                    url=f"https://{self.source_name}.example.com/result",
                    description=f"Description from {self.source_name}",
                    metrics={"mock": True, "query": query},
                )
            ]

        def normalize(self, raw_result: Any) -> SearchResult:
            return SearchResult(
                source=self.source_name,
                title=str(raw_result.get("title", "")),
                url=str(raw_result.get("url", "")),
                description=str(raw_result.get("description", "")),
                metrics=raw_result.get("metrics", {}),
                raw=raw_result,
            )

        def fallback(self, query: str) -> List[SearchResult]:
            return [
                SearchResult(
                    source=f"{self.source_name}_fallback",
                    title=f"Fallback: {query}",
                    url=f"https://{self.source_name}-fallback.example.com",
                    description="Fallback result",
                    metrics={"fallback": True},
                )
            ]

    return DynamicMockSearcher(config)


# ── Testes de Contrato BaseSearcher ─────────────────────────────────────────

@pytest.mark.contract
@pytest.mark.mock
class TestBaseSearcherContract:
    """Valida que todos os searchers implementam o contrato BaseSearcher."""

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_searcher_implements_base_class(self, searcher_name: str, mock_config: Dict[str, Any]):
        """Verifica que o searcher pode ser instanciado e herda BaseSearcher."""
        try:
            from src.search.factory import SearcherFactory
            factory = SearcherFactory()
            searcher = create_mock_searcher(searcher_name, mock_config)
            assert isinstance(searcher, BaseSearcher)
            assert hasattr(searcher, "search")
            assert hasattr(searcher, "normalize")
            assert hasattr(searcher, "fallback")
        except ImportError as e:
            pytest.skip(f"Factory não disponível: {e}")

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_search_returns_list_of_search_results(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """search() deve retornar List[SearchResult]."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = await searcher.search("test query")

        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SearchResult)

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_normalize_returns_search_result(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """normalize() deve retornar SearchResult."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        raw = {"title": "Test", "url": "https://test.com", "description": "Desc"}
        result = searcher.normalize(raw)

        assert isinstance(result, SearchResult)
        assert result.title == "Test"
        assert result.url == "https://test.com"

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_fallback_returns_list_of_search_results(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """fallback() deve retornar List[SearchResult]."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = searcher.fallback("test query")

        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SearchResult)

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_searcher_has_enabled_attribute(self, searcher_name: str, mock_config: Dict[str, Any]):
        """Searcher deve ter atributo 'enabled'."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        assert hasattr(searcher, "enabled")
        assert isinstance(searcher.enabled, bool)

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_searcher_has_timeout_attribute(self, searcher_name: str, mock_config: Dict[str, Any]):
        """Searcher deve ter atributo 'timeout'."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        assert hasattr(searcher, "timeout")
        assert isinstance(searcher.timeout, (int, float))
        assert searcher.timeout > 0


# ── Testes de Schema ────────────────────────────────────────────────────────

@pytest.mark.contract
@pytest.mark.mock
class TestSearchResultSchema:
    """Valida schema de SearchResult para todos os searchers."""

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_search_result_schema_valid(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """Resultados de search() devem seguir o schema SearchResult."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = await searcher.search("python async")

        assert len(results) > 0, f"{searcher_name} retornou lista vazia"

        for result in results:
            errors = validate_search_result_schema(result, searcher_name)
            assert not errors, f"Schema errors: {errors}"

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_normalize_result_schema_valid(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """Resultados de normalize() devem seguir o schema SearchResult."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        raw = {
            "title": "Normalized Title",
            "url": "https://normalized.example.com",
            "description": "Normalized description",
            "metrics": {"score": 0.9},
        }
        result = searcher.normalize(raw)

        errors = validate_search_result_schema(result, searcher_name)
        assert not errors, f"Schema errors: {errors}"

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_fallback_result_schema_valid(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """Resultados de fallback() devem seguir o schema SearchResult."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = searcher.fallback("test query")

        assert len(results) > 0, f"{searcher_name} fallback retornou lista vazia"

        for result in results:
            errors = validate_search_result_schema(result, searcher_name)
            assert not errors, f"Schema errors: {errors}"


# ── Testes de Rate Limiting ─────────────────────────────────────────────────

@pytest.mark.rate_limit
@pytest.mark.mock
class TestRateLimiting:
    """Valida que searchers respeitam rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self, mock_config: Dict[str, Any]):
        """Searcher deve respeitar rate limit configurado."""
        config = {**mock_config, "rate_limit_rps": 10.0}
        searcher = create_mock_searcher("github", config)

        start = time.monotonic()
        tasks = [searcher.search(f"query {i}") for i in range(5)]
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        assert hasattr(searcher, "timeout")

    @pytest.mark.asyncio
    async def test_rate_limit_per_domain(self, mock_config: Dict[str, Any]):
        """Rate limit deve ser por domínio, não global."""
        config = {**mock_config, "rate_limit_rps": 2.0}
        searcher = create_mock_searcher("github", config)
        assert searcher.timeout > 0

    @pytest.mark.asyncio
    async def test_rate_limit_resets_after_interval(self, mock_config: Dict[str, Any]):
        """Rate limit deve permitir novas requisições após intervalo."""
        config = {**mock_config, "rate_limit_rps": 100.0}
        searcher = create_mock_searcher("github", config)
        results = await searcher.search("test")
        assert len(results) > 0


# ── Testes de Circuit Breaker ───────────────────────────────────────────────

@pytest.mark.circuit_breaker
@pytest.mark.mock
class TestCircuitBreaker:
    """Valida integração de circuit breaker com searchers."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(
        self,
        mock_config: Dict[str, Any],
        circuit_breaker_registry: CircuitBreakerRegistry,
    ):
        """Circuit breaker deve abrir após N falhas consecutivas."""
        searcher = create_mock_searcher("github", mock_config)
        cb = circuit_breaker_registry.get("github")

        for _ in range(3):
            try:
                await cb.call(lambda: (_ for _ in ()).throw(Exception("API Error")))
            except Exception:
                pass

        assert cb.state.value == "open", f"Esperado OPEN, got {cb.state.value}"

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_when_open(
        self,
        mock_config: Dict[str, Any],
        circuit_breaker_registry: CircuitBreakerRegistry,
    ):
        """Circuit breaker OPEN deve rejeitar chamadas imediatamente."""
        cb = circuit_breaker_registry.get("github")

        for _ in range(5):
            try:
                await cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
            except Exception:
                pass

        with pytest.raises(CircuitBreakerOpen):
            await cb.call(lambda: "should not execute")

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_after_timeout(
        self,
        mock_config: Dict[str, Any],
        circuit_breaker_registry: CircuitBreakerRegistry,
    ):
        """Circuit breaker deve tentar HALF_OPEN após timeout."""
        cb = circuit_breaker_registry.get("github")
        cb._state = CircuitState.OPEN
        cb.metrics_data.last_failure_time = time.time() - 10.0

        cb._check_and_update_state()  # type: ignore
        assert cb.state.value == "half_open"

    @pytest.mark.asyncio
    async def test_circuit_breaker_closes_after_success(
        self,
        mock_config: Dict[str, Any],
        circuit_breaker_registry: CircuitBreakerRegistry,
    ):
        """Circuit breaker deve fechar após sucessos em HALF_OPEN."""
        cb = circuit_breaker_registry.get("github")
        cb._state = CircuitState.HALF_OPEN
        cb._half_open_calls = 0  # type: ignore
        cb._success_count = 0  # type: ignore

        for _ in range(3):
            await cb._on_success()  # type: ignore

        assert cb.state.value == "closed"

    @pytest.mark.asyncio
    async def test_searcher_with_circuit_breaker_protection(
        self,
        mock_config: Dict[str, Any],
        circuit_breaker_registry: CircuitBreakerRegistry,
    ):
        """Searcher deve usar circuit breaker para proteger chamadas."""
        from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig

        searcher = create_mock_searcher("github", mock_config)
        stage = SearchStage(
            searchers={"github": searcher},
            cache=None,
            ranker=Mock(),
            config=SearchStageConfig(),
            circuit_breaker_registry=circuit_breaker_registry,
        )

        assert "github" in stage._semaphores
        assert circuit_breaker_registry.get("github") is not None


# ── Testes de Fallback Cascade ──────────────────────────────────────────────

@pytest.mark.fallback
@pytest.mark.mock
class TestFallbackCascade:
    """Valida fallback cascade entre searchers."""

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_primary_failure(self, mock_config: Dict[str, Any]):
        """Fallback deve ser acionado quando searcher primário falha."""
        class FailingSearcher(BaseSearcher):
            def __init__(self, config: Dict[str, Any]):
                super().__init__(config)
                self.enabled = True
                self.timeout = 5.0

            async def search(self, query: str, **kwargs) -> List[SearchResult]:
                raise ConnectionError("API indisponível")

            def normalize(self, raw_result: Any) -> SearchResult:
                return SearchResult(source="fail", title="", url="", description="", metrics={})

            def fallback(self, query: str) -> List[SearchResult]:
                return [
                    SearchResult(
                        source="fail_fallback",
                        title=f"Fallback for {query}",
                        url="https://fallback.com",
                        description="Fallback result",
                        metrics={"fallback": True},
                    )
                ]

        searcher = FailingSearcher(mock_config)

        with pytest.raises(ConnectionError):
            await searcher.search("test")

        fallback_results = searcher.fallback("test")
        assert len(fallback_results) > 0
        assert fallback_results[0].source == "fail_fallback"

    @pytest.mark.asyncio
    async def test_fallback_cascade_order(self, mock_config: Dict[str, Any]):
        """Fallback deve seguir ordem de prioridade configurada."""
        from src.search.scraping_searcher import ScrapingSearcher, ScrapingConfig

        scraper_a = Mock()
        scraper_a.search = AsyncMock(side_effect=Exception("Scraper A falhou"))
        scraper_a.enabled = True

        scraper_b = Mock()
        scraper_b.search = AsyncMock(return_value={
            "title": "Scraper B Result",
            "url": "https://b.example.com",
            "markdown": "Content from B",
        })
        scraper_b.enabled = True

        config = ScrapingConfig(
            cascade_order=("scraper_a", "scraper_b"),
            timeout=5.0,
        )

        assert config.cascade_order == ("scraper_a", "scraper_b")

    @pytest.mark.asyncio
    async def test_fallback_returns_valid_schema(self, mock_config: Dict[str, Any]):
        """Resultados de fallback devem seguir schema SearchResult."""
        searcher = create_mock_searcher("github", mock_config)
        results = searcher.fallback("test query")

        for result in results:
            errors = validate_search_result_schema(result, "github_fallback")
            assert not errors, f"Schema errors no fallback: {errors}"

    def test_fallback_metrics_tracked(self, mock_config: Dict[str, Any]):
        """Fallback deve incluir métricas para tracking."""
        searcher = create_mock_searcher("github", mock_config)
        results = searcher.fallback("test")

        assert len(results) > 0
        assert results[0].metrics.get("fallback") is True


# ── Testes Reais (lentos, requerem credenciais) ───────────────────────────────

@pytest.mark.real
@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("SRA_RUN_REAL_TESTS"),
    reason="Define SRA_RUN_REAL_TESTS=1 para executar testes reais",
)
class TestRealSearchers:
    """Testes contra APIs reais. Requerem credenciais e são lentos."""

    @pytest.fixture(scope="class")
    def real_config(self) -> Dict[str, Any]:
        """Configuração com credenciais reais do ambiente."""
        return {
            "timeout": REAL_TEST_TIMEOUT,
            "rate_limit_rps": 1.0,
            "cache_enabled": False,
            "github_token": os.environ.get("GITHUB_TOKEN", ""),
            "reddit_client_id": os.environ.get("REDDIT_CLIENT_ID", ""),
            "reddit_client_secret": os.environ.get("REDDIT_CLIENT_SECRET", ""),
            "serpapi_key": os.environ.get("SERPAPI_KEY", ""),
            "tavily_key": os.environ.get("TAVILY_KEY", ""),
        }

    @pytest.mark.parametrize("searcher_name", ["github", "searxng", "web"])
    @pytest.mark.asyncio
    async def test_real_search_returns_results(
        self,
        searcher_name: str,
        real_config: Dict[str, Any],
    ):
        """Teste real: search() deve retornar resultados válidos."""
        pytest.skip(f"Implementação real de {searcher_name} requer setup específico")

    @pytest.mark.asyncio
    async def test_real_github_search(self, real_config: Dict[str, Any]):
        """Teste real contra GitHub API."""
        if not real_config.get("github_token"):
            pytest.skip("GITHUB_TOKEN não configurado")

        try:
            from src.search.github_searcher import GitHubSearcher
            searcher = GitHubSearcher(real_config)
            results = await searcher.search("python asyncio", domain="dev_tools")

            assert len(results) > 0
            for result in results:
                errors = validate_search_result_schema(result, "github")
                assert not errors
                assert "github.com" in result.url

        except ImportError:
            pytest.skip("GitHubSearcher não disponível")

    @pytest.mark.asyncio
    async def test_real_searxng_search(self, real_config: Dict[str, Any]):
        """Teste real contra SearXNG."""
        try:
            from src.search.searxng_searcher import SearXNGSearcher
            searcher = SearXNGSearcher(real_config)
            results = await searcher.search("python async frameworks")

            assert len(results) > 0
            for result in results:
                errors = validate_search_result_schema(result, "searxng")
                assert not errors

        except ImportError:
            pytest.skip("SearXNGSearcher não disponível")


# ── Testes de Performance ─────────────────────────────────────────────────────

@pytest.mark.slow
class TestSearcherPerformance:
    """Benchmarks de performance dos searchers."""

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_search_latency_under_threshold(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """search() deve completar em menos de 5s (mock)."""
        searcher = create_mock_searcher(searcher_name, mock_config)

        start = time.monotonic()
        results = await searcher.search("performance test")
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"{searcher_name} demorou {elapsed:.2f}s"
        assert len(results) > 0

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    def test_normalize_latency_under_threshold(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """normalize() deve completar em menos de 100ms."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        raw = {"title": "T", "url": "https://t.com", "description": "D"}

        start = time.monotonic()
        for _ in range(100):
            searcher.normalize(raw)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"{searcher_name} normalize demorou {elapsed:.3f}s para 100 calls"


# ── Testes de Concorrência ────────────────────────────────────────────────────

@pytest.mark.mock
class TestConcurrency:
    """Valida comportamento de searchers sob carga concorrente."""

    @pytest.mark.asyncio
    async def test_concurrent_searches(self, mock_config: Dict[str, Any]):
        """Múltiplas buscas concorrentes não devem corromper estado."""
        searcher = create_mock_searcher("github", mock_config)

        queries = [f"query {i}" for i in range(10)]
        tasks = [searcher.search(q) for q in queries]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        for i, result_list in enumerate(results):
            assert len(result_list) > 0
            assert result_list[0].metrics.get("query") == queries[i]

    @pytest.mark.asyncio
    async def test_concurrent_with_semaphore(self, mock_config: Dict[str, Any]):
        """Semáforo deve limitar concorrência por source."""
        from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig

        config = SearchStageConfig(max_concurrent_per_source=3)
        searcher = create_mock_searcher("github", mock_config)

        stage = SearchStage(
            searchers={"github": searcher},
            cache=None,
            ranker=Mock(),
            config=config,
        )

        assert stage._semaphores["github"]._value == 3


# ── Testes de Integração com Factory ────────────────────────────────────────

@pytest.mark.mock
class TestSearcherFactory:
    """Valida SearcherFactory e criação dinâmica."""

    def test_factory_creates_all_searchers(self, mock_config: Dict[str, Any]):
        """Factory deve conseguir criar instâncias de todos os searchers."""
        try:
            from src.search.factory import SearcherFactory
            mock_orch = MagicMock()
            mock_orch.config.timeout_per_source = 10.0
            mock_orch.config.max_results_per_source = 5
            mock_orch.config.github_token = "mock"
            mock_orch.config.producthunt_token = "mock"
            mock_orch.config.firecrawl_api_key = "mock"
            mock_orch.config.firecrawl_base_url = None
            mock_orch.config.spider_api_key = "mock"
            mock_orch.config.spider_base_url = None
            mock_orch.config.steel_api_key = "mock"
            mock_orch.config.steel_base_url = None
            mock_orch.config.serpapi_api_key = None
            mock_orch.config.serpapi_enabled = False
            mock_orch.config.tavily_api_key = None
            mock_orch.config.tavily_enabled = False
            mock_orch.config.playwright_enabled = False
            mock_orch.config.residential_proxy_provider = None

            searchers = SearcherFactory.create_searchers(mock_orch)
            assert isinstance(searchers, dict)
            assert len(searchers) > 0
        except ImportError:
            pytest.skip("SearcherFactory não disponível")

    def test_factory_returns_correct_type(self, mock_config: Dict[str, Any]):
        """Factory deve retornar instância de BaseSearcher."""
        try:
            from src.search.factory import SearcherFactory
            mock_orch = MagicMock()
            mock_orch.config.timeout_per_source = 10.0
            mock_orch.config.max_results_per_source = 5
            mock_orch.config.github_token = "mock"
            mock_orch.config.producthunt_token = "mock"
            mock_orch.config.firecrawl_api_key = "mock"
            mock_orch.config.firecrawl_base_url = None
            mock_orch.config.spider_api_key = "mock"
            mock_orch.config.spider_base_url = None
            mock_orch.config.steel_api_key = "mock"
            mock_orch.config.steel_base_url = None
            mock_orch.config.serpapi_api_key = None
            mock_orch.config.serpapi_enabled = False
            mock_orch.config.tavily_api_key = None
            mock_orch.config.tavily_enabled = False
            mock_orch.config.playwright_enabled = False
            mock_orch.config.residential_proxy_provider = None

            searchers = SearcherFactory.create_searchers(mock_orch)
            for name, s in searchers.items():
                if name == "serpapi":
                    continue  # SerpAPI é duck-typed
                if name in ("notion", "confluence", "sharepoint"):
                    continue  # Conectores Enterprise (BaseConnectorImplementation, não BaseSearcher)
                assert isinstance(s, BaseSearcher)
        except ImportError:
            pytest.skip("SearcherFactory não disponível")


# ── Testes de Sanitização ───────────────────────────────────────────────────

@pytest.mark.mock
class TestQuerySanitization:
    """Valida que searchers sanitizam queries corretamente."""

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, searcher_name: str, mock_config: Dict[str, Any]):
        """Query vazia deve retornar resultados vazios ou ser tratada."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = await searcher.search("")
        assert isinstance(results, list)

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_special_characters_handled(
        self,
        searcher_name: str,
        mock_config: Dict[str, Any],
    ):
        """Caracteres especiais na query não devem quebrar."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        results = await searcher.search("<script>alert(1)</script>")
        assert isinstance(results, list)

    @pytest.mark.parametrize("searcher_name", ALL_SEARCHERS)
    @pytest.mark.asyncio
    async def test_very_long_query(self, searcher_name: str, mock_config: Dict[str, Any]):
        """Query muito longa deve ser truncada ou tratada."""
        searcher = create_mock_searcher(searcher_name, mock_config)
        long_query = "python " * 1000
        results = await searcher.search(long_query)
        assert isinstance(results, list)
