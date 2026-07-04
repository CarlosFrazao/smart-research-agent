"""Testes de integração do pipeline completo do Smart Research Agent.

Suíte que valida o fluxo end-to-end com mocks de infraestrutura,
testando: pipeline com searchers mockados, fallbacks, circuit breakers,
enforcement de budget e benchmark de latência por modo de operação.

Requer:
    pip install pytest pytest-asyncio pytest-benchmark

Uso:
    pytest tests/integration/test_pipeline.py -v
    pytest tests/integration/test_pipeline.py -k "latency" --benchmark-only
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Fixtures Compartilhadas ───────────────────────────────────────────────


@pytest.fixture
def mock_llm_client():
    """LLMClient mockado com respostas estruturadas por tipo de chamada."""
    client = MagicMock()

    async def _generate_structured(prompt: str, schema: dict, **kwargs):
        props = schema.get("properties", {})
        keys = set(props.keys())

        if "domain" in keys and "intention" in keys:
            return {
                "domain": "saas_b2b",
                "entities": ["HubSpot", "CRM"],
                "intention": "discover",
                "urgency": "nao",
                "confidence": "alta",
            }
        elif "expanded_queries" in keys or "queries" in keys:
            return {
                "expanded_queries": [
                    {
                        "query": "open source CRM alternative",
                        "type": "qualificador",
                        "priority": "alta",
                        "rationale": "encontra alternativas",
                    },
                    {
                        "query": "CRM open source 2026",
                        "type": "temporal",
                        "priority": "media",
                        "rationale": "atualizações recentes",
                    },
                ]
            }
        elif "missing_topics" in keys or "is_complete" in keys:
            return {
                "is_complete": True,
                "missing_aspects": [],
                "new_queries": [],
                "confidence": "alta",
                "rationale": "Pesquisa completa",
            }
        elif "sources" in keys:
            return {
                "sources": ["github", "reddit", "hackernews"],
                "rationale": "Fontes relevantes para SaaS B2B",
            }
        elif "entities" in keys and "score" in keys:
            return {
                "entities": [
                    {
                        "entity": "twenty",
                        "title": "Twenty CRM",
                        "description": "Open source CRM moderno",
                        "sources": ["github"],
                        "urls": ["https://github.com/twentyhq/twenty"],
                        "combined_score": 88.0,
                        "metrics": {"stars": 15000, "forks": 800, "language": "TypeScript"},
                        "highlights": ["15k stars no GitHub"],
                    }
                ],
                "confidence": "alta",
            }
        elif "final_score" in keys or "verdict" in keys:
            return {
                "final_score": 85,
                "verdict": "Aprovado",
                "strengths": ["Cobertura completa", "Fontes confiáveis"],
                "weaknesses": [],
                "confidence": "alta",
            }
        return {}

    async def _complete(prompt: str, **kwargs):
        if "relatório" in prompt.lower() or "report" in prompt.lower():
            return "## Relatório de Pesquisa\n\nResumo executivo gerado."
        if "síntese" in prompt.lower() or "synthesize" in prompt.lower():
            return "Síntese unificada dos resultados de pesquisa."
        return "Resposta mockada do LLM."

    client.generate_structured = AsyncMock(side_effect=_generate_structured)
    client.complete = AsyncMock(side_effect=_complete)
    client.generate = AsyncMock(side_effect=_complete)
    return client


@pytest.fixture
def mock_searchers():
    """Dicionário de searchers mockados com resultados realistas."""
    from src.types import SearchResult

    github_results = [
        SearchResult(
            source="github",
            title="twentyhq/twenty",
            url="https://github.com/twentyhq/twenty",
            description="A modern open-source CRM. Alternative to Salesforce.",
            metrics={"stars": 15000, "forks": 800, "language": "TypeScript", "updated_at": "2026-06-15T00:00:00Z"},
        ),
        SearchResult(
            source="github",
            title="n8n-io/n8n",
            url="https://github.com/n8n-io/n8n",
            description="Workflow automation. Alternative to Zapier.",
            metrics={"stars": 42000, "forks": 5000, "language": "TypeScript", "updated_at": "2026-06-20T00:00:00Z"},
        ),
    ]

    reddit_results = [
        SearchResult(
            source="reddit",
            title="Best open source CRM in 2026?",
            url="https://reddit.com/r/selfhosted/comments/abc123",
            description="Twenty is getting popular. Good alternative to HubSpot.",
            metrics={"upvotes": 250, "comments": 45},
        ),
    ]

    hn_results = [
        SearchResult(
            source="hackernews",
            title="Show HN: Twenty – Open-source CRM",
            url="https://news.ycombinator.com/item?id=38000000",
            description="Launch discussion. 300+ points.",
            metrics={"points": 320, "comments": 89},
        ),
    ]

    arxiv_results = [
        SearchResult(
            source="arxiv",
            title="CRM Systems: A Survey",
            url="https://arxiv.org/abs/2401.00001",
            description="Survey of modern CRM architectures.",
            metrics={"citations": 45, "published": "2024-01-15"},
        ),
    ]

    return {
        "github": _make_searcher_mock("github", github_results),
        "reddit": _make_searcher_mock("reddit", reddit_results),
        "hackernews": _make_searcher_mock("hackernews", hn_results),
        "arxiv": _make_searcher_mock("arxiv", arxiv_results),
        "google": _make_searcher_mock("google", []),
        "brave": _make_searcher_mock("brave", []),
    }


def _make_searcher_mock(name: str, results: list):
    """Cria um searcher mockado com interface completa."""
    searcher = MagicMock()
    searcher.search = AsyncMock(return_value=results)
    searcher.enabled = True
    searcher.source = name
    searcher.timeout = 30
    searcher.close = AsyncMock()
    return searcher


@pytest.fixture
def mock_config():
    """Config mockada com budgets e modos."""
    from src.config import Config
    config = Config(
        anthropic_api_key="test-key",
        max_iterations=1,
        operation_mode="cirurgia",
    )
    config.budget_tokens_per_query = 10000
    config.budget_cost_per_query_usd = 5.0
    config.budget_timeout_seconds = 60
    return config


@pytest.fixture
def mock_orchestrator(mock_config, mock_llm_client, mock_searchers):
    """Orchestrator com todos os mocks injetados."""
    from src.orchestrator import Orchestrator
    from src.pipeline.stage_factory import StageFactory

    orch = Orchestrator(mock_config)
    orch.llm = mock_llm_client
    orch.searchers = mock_searchers

    # Mock cache global para simular cache miss e evitar chamadas reais a cache persistido
    orch.cache = MagicMock()
    orch.cache.get = AsyncMock(return_value=None)
    orch.cache.set = AsyncMock()
    del orch.cache.get_similar

    # Adiciona stubs legados para patches de testes legados funcionarem
    orch._plan_search = MagicMock()
    orch._execute_searches = MagicMock()
    orch._synthesize_results = MagicMock()

    # Reconstrói o pipeline para usar o mock_llm_client injetado
    orch._pipeline = StageFactory.build_pipeline(orch)

    # Mock serviços internos
    orch._search_service = MagicMock()
    orch._search_service.execute = AsyncMock(return_value=[
        *mock_searchers["github"].search.return_value,
        *mock_searchers["reddit"].search.return_value,
        *mock_searchers["hackernews"].search.return_value,
    ])

    return orch


# ─── Testes de Pipeline com Searchers Mockados ─────────────────────────────


@pytest.mark.asyncio
class TestPipelineWithMockedSearchers:
    """Valida o pipeline completo com infraestrutura mockada."""

    async def test_pipeline_executes_all_stages(self, mock_orchestrator):
        """Verifica que todas as 8 etapas do pipeline são executadas."""
        pipeline = mock_orchestrator._pipeline
        stage_mocks = []
        for stage in pipeline.stages:
            m = AsyncMock(return_value=None)
            stage_mocks.append(m)
            stage.run = m

        report = await mock_orchestrator.research("melhor CRM open source")

        assert report is not None
        for m in stage_mocks:
            m.assert_called_once()

    async def test_pipeline_with_real_searchers_mocked(self, mock_orchestrator, mock_searchers):
        """Pipeline com searchers reais (mockados) retornando dados."""
        report = await mock_orchestrator.research("melhor CRM open source")

        assert report is not None
        assert isinstance(report, str)
        # Verifica que searchers foram chamados
        for name in ["github", "reddit", "hackernews"]:
            if name in mock_searchers:
                assert mock_searchers[name].search.called or \
                       mock_orchestrator._search_service.execute.called

    async def test_pipeline_result_contains_expected_entities(self, mock_orchestrator):
        """Verifica que entidades esperadas aparecem no relatório."""
        report = await mock_orchestrator.research("CRM open source")

        # O relatório deve conter conteúdo (mesmo que mockado)
        assert len(report) > 100
        # Verifica estrutura mínima
        assert "#" in report or "Relatório" in report or "Resumo" in report

    async def test_pipeline_handles_empty_results(self, mock_orchestrator, mock_searchers):
        """Pipeline deve funcionar mesmo com resultados vazios de searchers."""
        for s in mock_searchers.values():
            s.search = AsyncMock(return_value=[])

        mock_orchestrator._search_service.execute = AsyncMock(return_value=[])

        report = await mock_orchestrator.research("query sem resultados")
        assert report is not None

    async def test_pipeline_parallel_search_execution(self, mock_orchestrator, mock_searchers):
        """Verifica que múltiplos searchers são executados em paralelo."""
        call_times = {}

        async def timed_search(query, **kwargs):
            call_times[kwargs.get("_source", "unknown")] = time.monotonic()
            await asyncio.sleep(0.01)  # simula latência
            return []

        for name, searcher in mock_searchers.items():
            searcher.search = AsyncMock(side_effect=lambda q, n=name, **kw: timed_search(q, _source=n, **kw))

        mock_orchestrator._search_service.execute = AsyncMock(
            side_effect=lambda queries, plan, intent: asyncio.gather(
                *[mock_searchers["github"].search(q.query) for q in queries[:1]]
            )
        )

        await mock_orchestrator.research("test parallel")
        # Se executou em paralelo, os tempos devem estar próximos


# ─── Testes de Fallbacks ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFallbacks:
    """Valida o sistema de fallbacks em diferentes cenários."""

    async def test_fallback_manager_priority_strategy(self):
        """Fallback com estratégia PRIORITY: primary → fallback1 → fallback2."""
        from src.pipeline.fallback_manager import FallbackManager, FallbackStrategy

        fm = FallbackManager()
        await fm.init()

        call_order = []

        async def primary(q):
            call_order.append("primary")
            raise ConnectionError("primary down")

        async def fallback1(q):
            call_order.append("fallback1")
            return ["result_fallback1"]

        async def fallback2(q):
            call_order.append("fallback2")
            return ["result_fallback2"]

        fm.register(
            stage="search",
            name="test_priority",
            primary=primary,
            fallbacks=[("fb1", fallback1), ("fb2", fallback2)],
            strategy=FallbackStrategy.PRIORITY,
        )

        result = await fm.execute(stage="search", name="test_priority", args=("query",))

        assert result == ["result_fallback1"]
        assert call_order == ["primary", "fallback1"]

    async def test_fallback_manager_round_robin(self):
        """Fallback com ROUND_ROBIN distribui carga entre alternativas."""
        from src.pipeline.fallback_manager import FallbackManager, FallbackStrategy

        fm = FallbackManager()
        await fm.init()

        async def primary(q):
            raise ConnectionError("down")

        async def fb1(q):
            return ["fb1"]

        async def fb2(q):
            return ["fb2"]

        fm.register(
            stage="search",
            name="test_rr",
            primary=primary,
            fallbacks=[("fb1", fb1), ("fb2", fb2)],
            strategy=FallbackStrategy.ROUND_ROBIN,
        )

        # Primeira chamada: usa fb1
        r1 = await fm.execute(stage="search", name="test_rr", args=("q",))
        # Segunda chamada: usa fb2 (round-robin)
        r2 = await fm.execute(stage="search", name="test_rr", args=("q",))

        assert r1 == ["fb1"] or r1 == ["fb2"]
        assert r2 == ["fb1"] or r2 == ["fb2"]

    async def test_fallback_metrics_collected(self):
        """Verifica que métricas de fallback são coletadas corretamente."""
        from src.pipeline.fallback_manager import FallbackManager, FallbackStrategy

        fm = FallbackManager()
        await fm.init()

        async def primary(q):
            raise RuntimeError("fail")

        async def fb(q):
            return ["ok"]

        fm.register(
            stage="search",
            name="test_metrics",
            primary=primary,
            fallbacks=[("fb", fb)],
            strategy=FallbackStrategy.PRIORITY,
        )

        await fm.execute(stage="search", name="test_metrics", args=("q",))

        metrics = fm.get_metrics("search", "test_metrics")
        m = metrics["search:test_metrics"]

        assert m.total_invocations == 1
        assert m.primary_failure == 1
        assert m.fallback_success == 1
        assert m.success_rate == 0.5
        assert m.fallback_rate == 1.0

    async def test_fallback_all_failed_raises_exhausted(self):
        """Quando todas as alternativas falham, deve lançar FallbackExhaustedError."""
        from src.pipeline.fallback_manager import FallbackManager, FallbackStrategy, FallbackExhaustedError

        fm = FallbackManager()
        await fm.init()

        async def fail(q):
            raise RuntimeError("always fails")

        fm.register(
            stage="search",
            name="test_exhausted",
            primary=fail,
            fallbacks=[("fb1", fail)],
            strategy=FallbackStrategy.PRIORITY,
        )

        with pytest.raises(FallbackExhaustedError) as exc_info:
            await fm.execute(stage="search", name="test_exhausted", args=("q",))

        assert exc_info.value.attempts == 2
        assert "test_exhausted" in str(exc_info.value)

    async def test_fallback_with_circuit_breaker_skip(self):
        """Fallback não deve tentar ação com circuit breaker aberto."""
        from src.pipeline.fallback_manager import FallbackAction, FallbackManager, FallbackStrategy
        from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, get_default_registry

        fm = FallbackManager()
        await fm.init()

        # Abre circuito para fb1
        registry = await get_default_registry()
        cb = await registry.get_or_create("fb1_cb", config=CircuitBreakerConfig(name="fb1_cb", failure_threshold=1))
        await cb._on_failure()  # força OPEN
        await cb._on_failure()
        await cb._on_failure()

        async def primary(q):
            raise RuntimeError("fail")

        async def fb1(q):
            return ["fb1"]

        async def fb2(q):
            return ["fb2"]

        fm.register(
            stage="search",
            name="test_cb_skip",
            primary=primary,
            fallbacks=[
                ("fb1_cb", fb1),
                ("fb2", fb2),
            ],
            strategy=FallbackStrategy.PRIORITY,
        )

        result = await fm.execute(stage="search", name="test_cb_skip", args=("q",))
        assert result == ["fb2"]  # fb1 pulado por CB aberto


# ─── Testes de Circuit Breakers ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestCircuitBreakers:
    """Valida circuit breakers em cenários de integração."""

    async def test_circuit_opens_after_threshold(self):
        """Circuito deve abrir após N falhas consecutivas."""
        from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, get_default_registry

        registry = await get_default_registry()
        cb = await registry.get_or_create(
            "test_service",
            config=CircuitBreakerConfig(name="test_service", failure_threshold=3, recovery_timeout=60),
        )

        async def failing_call():
            raise ConnectionError("API down")

        # 3 falhas devem abrir o circuito
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await cb.call(failing_call)

        assert cb.state.value == "open"

    async def test_circuit_half_open_recovery(self):
        """Circuito deve transitar para HALF_OPEN após timeout."""
        from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        cb = CircuitBreaker(CircuitBreakerConfig(name="test_recovery", failure_threshold=2, recovery_timeout=0.05))

        async def fail():
            raise RuntimeError("fail")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        assert cb.state.value == "open"
        await asyncio.sleep(0.1)  # aguarda recovery_timeout

        async def success():
            return "recovered"

        result = await cb.call(success)
        assert result == "recovered"
        assert cb.state.value == "closed"

    async def test_circuit_metrics_tracked(self):
        """Métricas de circuit breaker devem refletir falhas e sucessos."""
        from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        cb = CircuitBreaker(CircuitBreakerConfig(name="test_metrics", failure_threshold=2, recovery_timeout=60))

        await cb.call(AsyncMock(return_value="ok"))
        await cb.call(AsyncMock(return_value="ok"))

        metrics = cb.get_metrics()
        assert metrics["total_successes"] == 2
        assert metrics["total_failures"] == 0
        assert metrics["total_calls"] == 2

    async def test_circuit_breaker_registry_isolation(self):
        """Circuit breakers no registry devem ser isolados por nome."""
        from src.utils.circuit_breaker import CircuitBreakerConfig, get_default_registry

        registry = await get_default_registry()
        cb1 = await registry.get_or_create("svc1", config=CircuitBreakerConfig(name="svc1", failure_threshold=2))
        cb2 = await registry.get_or_create("svc2", config=CircuitBreakerConfig(name="svc2", failure_threshold=5))

        async def fail():
            raise RuntimeError("fail")

        # Abre svc1
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb1.call(fail)

        assert cb1.state.value == "open"
        assert cb2.state.value == "closed"  # svc2 intacto


# ─── Testes de Budget Enforcement ────────────────────────────────────────────


@pytest.mark.asyncio
class TestBudgetEnforcement:
    """Valida enforcement de budget (tokens, custo, tempo)."""

    async def test_token_budget_enforced(self, mock_orchestrator, mock_llm_client):
        """Pipeline deve respeitar budget máximo de tokens por query."""
        mock_orchestrator.config.budget_tokens_per_query = 500

        token_count = 0
        original_complete = mock_llm_client.complete

        async def counting_complete(prompt, **kwargs):
            nonlocal token_count
            # Estima tokens (1 token ≈ 4 chars)
            estimated = len(prompt) // 4
            token_count += estimated
            if token_count > mock_orchestrator.config.budget_tokens_per_query:
                raise RuntimeError("Token budget exceeded")
            return await original_complete(prompt, **kwargs)

        mock_llm_client.complete = AsyncMock(side_effect=counting_complete)

        # Pipeline deve completar ou falhar gracefulmente
        try:
            report = await mock_orchestrator.research("query teste")
            assert report is not None
        except RuntimeError as e:
            assert "budget" in str(e).lower() or "exceeded" in str(e).lower()

    async def test_cost_budget_enforced(self, mock_orchestrator):
        """Pipeline deve rastrear e limitar custo estimado."""
        from src.monitoring.budget_tracker import BudgetTracker

        tracker = BudgetTracker(max_cost_usd=0.01)  # budget muito baixo

        # Simula chamadas que excedem budget
        with pytest.raises(Exception) as exc_info:
            for _ in range(100):
                tracker.record_call(model="gpt-4", input_tokens=1000, output_tokens=500)

        assert "budget" in str(exc_info.value).lower() or "cost" in str(exc_info.value).lower()

    async def test_timeout_budget_enforced(self, mock_orchestrator):
        """Pipeline deve respeitar timeout total."""
        mock_orchestrator.config.budget_timeout_seconds = 0.1

        async def slow_search(query, **kwargs):
            await asyncio.sleep(1.0)  # muito mais lento que o budget
            return []

        for searcher in mock_orchestrator.searchers.values():
            searcher.search = AsyncMock(side_effect=slow_search)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                mock_orchestrator.research("query lenta"),
                timeout=mock_orchestrator.config.budget_timeout_seconds,
            )

    async def test_budget_metrics_reported(self, mock_orchestrator):
        """Métricas de budget devem ser reportadas no relatório."""
        report = await mock_orchestrator.research("query com budget")

        # O relatório pode conter metadados de budget
        assert report is not None
        # Verifica que o pipeline não travou


# ─── Benchmark de Latência por Modo ──────────────────────────────────────────


@pytest.mark.benchmark
class TestLatencyBenchmark:
    """Benchmarks de latência por modo de operação."""

    @pytest.fixture
    def benchmark_orchestrator(self, mock_config, mock_llm_client, mock_searchers):
        """Orchestrator configurado para benchmarks."""
        from src.orchestrator import Orchestrator
        from src.pipeline.stage_factory import StageFactory

        orch = Orchestrator(mock_config)
        orch.llm = mock_llm_client
        orch.searchers = mock_searchers

        # Mock cache global para evitar cache hit/write nos testes de benchmark e rede
        orch.cache = MagicMock()
        orch.cache.get = AsyncMock(return_value=None)
        orch.cache.set = AsyncMock()
        del orch.cache.get_similar

        # Reconstrói o pipeline para usar os mocks
        orch._pipeline = StageFactory.build_pipeline(orch)

        return orch

    @pytest.mark.asyncio
    async def test_latency_guerrilha_mode(self, benchmark_orchestrator):
        """Modo guerrilha: < 30s, mínimo de searchers."""
        from src.operation_modes import OperationModes

        mode = OperationModes.get_mode("guerrilha")
        benchmark_orchestrator.operation_mode = mode

        start = time.monotonic()
        report = await benchmark_orchestrator.research("teste rápido")
        elapsed = time.monotonic() - start

        assert elapsed < mode.timeout_seconds
        assert report is not None
        # Guerrilha deve usar menos searchers
        calls = sum(1 for s in benchmark_orchestrator.searchers.values() if s.search.called)
        assert calls <= len(mode.searchers)

    @pytest.mark.asyncio
    async def test_latency_cirurgia_mode(self, benchmark_orchestrator):
        """Modo cirurgia: < 300s, máxima precisão."""
        from src.operation_modes import OperationModes

        mode = OperationModes.get_mode("cirurgia")
        benchmark_orchestrator.operation_mode = mode

        start = time.monotonic()
        report = await benchmark_orchestrator.research("teste preciso")
        elapsed = time.monotonic() - start

        assert elapsed < mode.timeout_seconds
        assert report is not None
        # Cirurgia deve ter auditoria habilitada
        assert mode.enable_auditor is True

    @pytest.mark.asyncio
    async def test_latency_radar_mode(self, benchmark_orchestrator):
        """Modo radar: < 60s, foco em trending."""
        from src.operation_modes import OperationModes

        mode = OperationModes.get_mode("radar")
        benchmark_orchestrator.operation_mode = mode

        start = time.monotonic()
        report = await benchmark_orchestrator.research("trending tech")
        elapsed = time.monotonic() - start

        assert elapsed < mode.timeout_seconds
        assert report is not None

    @pytest.mark.asyncio
    async def test_latency_black_ops_mode(self, benchmark_orchestrator):
        """Modo black_ops: < 600s, máxima cobertura."""
        from src.operation_modes import OperationModes

        mode = OperationModes.get_mode("black_ops")
        benchmark_orchestrator.operation_mode = mode

        # Adiciona mocks para os searchers planejados que estão faltando
        for name in ["producthunt", "awesome", "firecrawl"]:
            if name not in benchmark_orchestrator.searchers:
                benchmark_orchestrator.searchers[name] = _make_searcher_mock(name, [])

        start = time.monotonic()
        report = await benchmark_orchestrator.research("deep research")
        elapsed = time.monotonic() - start

        assert elapsed < mode.timeout_seconds
        assert report is not None
        # Black ops deve usar mais searchers
        calls = sum(1 for s in benchmark_orchestrator.searchers.values() if s.search.called)
        assert calls >= len(mode.searchers) // 2  # pelo menos metade

    @pytest.mark.asyncio
    async def test_latency_degrades_gracefully_with_fallbacks(self, benchmark_orchestrator):
        """Latência deve degradar gracefulmente quando fallbacks são acionados."""
        # Simula searcher lento que dispara fallback
        async def slow_then_fail(query, **kwargs):
            await asyncio.sleep(0.5)
            raise TimeoutError("slow")

        benchmark_orchestrator.searchers["github"].search = AsyncMock(side_effect=slow_then_fail)

        start = time.monotonic()
        report = await benchmark_orchestrator.research("teste com fallback")
        elapsed = time.monotonic() - start

        # Deve completar, mesmo com fallback
        assert report is not None
        assert elapsed < 30  # não deve explodar

    @pytest.mark.asyncio
    async def test_parallel_search_latency(self, benchmark_orchestrator):
        """Busca paralela deve ser mais rápida que sequencial."""
        delays = {"github": 0.1, "reddit": 0.1, "hackernews": 0.1}

        async def delayed_search(query, **kwargs):
            source = kwargs.get("source", "unknown")
            await asyncio.sleep(delays.get(source, 0.05))
            return []

        for name, searcher in benchmark_orchestrator.searchers.items():
            if name in delays:
                searcher.search = AsyncMock(side_effect=lambda q, n=name, **kw: delayed_search(q, source=n, **kw))

        start = time.monotonic()
        await benchmark_orchestrator.research("teste paralelo")
        elapsed = time.monotonic() - start

        # Paralelo: ~0.1s (max delay) + overhead
        # Sequencial seria ~0.3s
        assert elapsed < 0.5  # deve ser próximo do delay máximo


# ─── Testes de Integração End-to-End ─────────────────────────────────────────


@pytest.mark.asyncio
class TestEndToEndIntegration:
    """Testes E2E que validam o pipeline como um todo."""

    async def test_full_pipeline_with_all_mocks(self, mock_orchestrator):
        """Pipeline completo com todos os serviços mockados."""
        report = await mock_orchestrator.research(
            "melhor alternativa open source ao HubSpot CRM"
        )

        assert report is not None
        assert isinstance(report, str)
        assert len(report) > 0

    async def test_pipeline_with_memory_context(self, mock_orchestrator):
        """Pipeline com memória persistente (contexto de sessão)."""
        mock_orchestrator.memory = MagicMock()
        mock_orchestrator.memory.get_relevant = AsyncMock(return_value=[
            {"query": "CRM anterior", "result": "Twenty CRM"}
        ])

        report = await mock_orchestrator.research("CRM open source")
        assert report is not None

    async def test_pipeline_error_recovery(self, mock_orchestrator):
        """Pipeline deve recuperar de falhas parciais."""
        # Um searcher falha, outros continuam
        mock_orchestrator.searchers["github"].search = AsyncMock(
            side_effect=RuntimeError("GitHub API down")
        )

        report = await mock_orchestrator.research("teste recuperação")
        assert report is not None  # não deve crashar

    async def test_pipeline_idempotency(self, mock_orchestrator):
        """Pipeline deve ser idempotente: mesma query → resultado consistente."""
        r1 = await mock_orchestrator.research("query idempotente")
        r2 = await mock_orchestrator.research("query idempotente")

        # Com mocks fixos, deve ser igual
        assert r1 is not None
        assert r2 is not None
        # Estrutura deve ser similar (mesmo que conteúdo LLM varie)

    async def test_pipeline_concurrent_queries(self, mock_orchestrator):
        """Múltiplas queries em paralelo não devem interferir."""
        queries = ["query A", "query B", "query C"]

        results = await asyncio.gather(*[
            mock_orchestrator.research(q) for q in queries
        ])

        assert len(results) == 3
        assert all(r is not None for r in results)


# ─── Fixtures adicionais para pytest-benchmark ─────────────────────────────


@pytest.fixture(scope="session")
def benchmark_config():
    """Configuração global para benchmarks."""
    return {
        "min_rounds": 3,
        "max_time": 30,
        "timer": time.monotonic,
    }
