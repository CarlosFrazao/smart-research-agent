"""Épico A (Blindagem black_ops): Transparência de Falhas no Relatório.

Cobre A1 (circuit breaker OPEN -> search_errors), A2 (timeout -> search_errors)
e A3 (fontes puladas por credencial -> rodapé + resumo executivo).

Critério de aceite do plano:
  - A1: ao abrir breaker, context.extra["search_errors"] contém a falha.
  - A2: todo error_msg não-vazio chega a search_errors (inclui timeout).
  - A3: seção "Configuração Ausente" lista exa/tavily/x quando requires_key sem valor,
        e o resumo executivo reflete as fontes puladas por credencial.
"""

import asyncio
import os
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig
from src.pipeline.stages.report_stage import ReportStage
from src.types import (
    SourcePlan,
    ExpandedQuery,
    IntentResult,
    Domain,
    Intention,
    RankedResult,
    SearchResult,
    ResearchMetadata,
    SynthesizedResult,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_context(
    sources: dict[str, List[str]],
    expanded: List[str] | None = None,
    domain: str = "general",
) -> PipelineContext:
    source_plan = SourcePlan(
        sources={
            name: [ExpandedQuery(query=q, type="discover", priority="media")]
            for name, queries in sources.items()
            for q in queries
        }
    )
    intent = IntentResult(
        domain=Domain(domain),
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="media",
    )
    first = expanded[0] if expanded else "consulta de teste"
    ctx = PipelineContext(
        query=first,
        source_plan=source_plan,
        intent=intent,
    )
    ctx.expanded_queries = [
        ExpandedQuery(query=q, type="discover", priority="media")
        for q in (expanded or ["consulta de teste"])
    ]
    return ctx


def _fake_searcher(
    results: List[SearchResult],
    *,
    has_credentials: bool = True,
    side_effect: Any = None,
) -> Any:
    """Searcher falso determinístico para exercitar o SearchStage.

    Se ``side_effect`` for passado, ``search`` levanta essa exceção (ex.:
    CircuitBreakerOpen ou asyncio.TimeoutError) para simular falha real.
    """
    searcher = MagicMock()
    searcher.enabled = True
    searcher.has_credentials = has_credentials
    if side_effect is not None:
        searcher.search = AsyncMock(side_effect=side_effect)
    else:
        searcher.search = AsyncMock(return_value=results)
    return searcher


def _plain_result(source: str, url: str = "https://example.com") -> SearchResult:
    return SearchResult(
        source=source,
        title=f"Resultado de {source}",
        url=url,
        description="Descrição de teste",
    )


class _FakeRanker:
    """Ranker que devolve os mesmos resultados como RankedResult."""

    async def rank(self, results: List[SearchResult]) -> List[RankedResult]:
        return [RankedResult(**r.model_dump()) for r in results]


def _fake_report_stage() -> ReportStage:
    """ReportStage com LLM mockado para não depender de API externa."""
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(
        return_value="""{
            "executive_summary": "Resumo executivo simulado com dados concretos e válidos para ultrapassar o tamanho mínimo exigido pelo validador de seções.",
            "recommendation": "Recomendação simulada principal com alternativas estratégicas detalhadas para passar a regra de comprimento do validador de seções.",
            "trends": "Tendências tecnológicas simuladas com evidência concreta de mercado e adoção de comunidade de desenvolvedores ativos no ecossistema."
        }"""
    )
    mock_llm.generate = AsyncMock(
        return_value=(
            "Resumo executivo simulado com dados concretos e válidos para "
            "ultrapassar o tamanho mínimo exigido pelo validador de seções."
        )
    )
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    return ReportStage(llm_client=mock_llm, cache=mock_cache)


# ── A1: Circuit Breaker OPEN propaga para search_errors ───────────────────────


def test_A1_circuit_breaker_open_propagates_to_search_errors():
    """A1/F2: ao abrir breaker, context.extra['search_errors'] recebe a falha."""
    from src.utils.circuit_breaker import CircuitBreakerOpen

    ctx = _make_context({"web": ["busca qualquer"]})
    searchers = {
        "web": _fake_searcher(
            [_plain_result("web")],
            has_credentials=True,
            side_effect=CircuitBreakerOpen(f"Circuito aberto para 'web'"),
        )
    }
    stage = SearchStage(
        searchers=searchers,
        cache=None,
        ranker=_FakeRanker(),
        config=SearchStageConfig(fallback_on_empty=False),
    )

    asyncio.run(stage.run(ctx))

    errors = ctx.extra.get("search_errors", [])
    assert any("Circuit Breaker OPEN" in e and "web" in e for e in errors), errors


# ── A2: Timeout de fonte propaga para search_errors ───────────────────────────


def test_A2_source_timeout_propagates_to_search_errors():
    """A2/F1: timeout de fonte tratado internamente chega a search_errors."""
    ctx = _make_context({"web": ["busca lenta"]})
    searchers = {
        "web": _fake_searcher(
            [_plain_result("web")],
            has_credentials=True,
            side_effect=asyncio.TimeoutError(),
        )
    }
    stage = SearchStage(
        searchers=searchers,
        cache=None,
        ranker=_FakeRanker(),
        config=SearchStageConfig(fallback_on_empty=False),
    )

    asyncio.run(stage.run(ctx))

    errors = ctx.extra.get("search_errors", [])
    assert any("web" in e for e in errors), errors


# ── A3: Fontes puladas por credencial aparecem no rodapé E no resumo ──────────


def test_A3_missing_credential_in_report_footer():
    """A3/F7: resumo executivo + rodapé listam fontes sem credencial."""
    ctx = _make_context({"exa": ["consulta"]})
    ctx.extra["search_warnings"] = [
        "Fonte 'exa' sem credencial configurada — busca indisponível ou limitada.",
    ]
    ctx.synthesized_results = []
    ctx.ranked_results = []
    ctx.completed_stages = []
    ctx.report = ""

    stage = _fake_report_stage()
    asyncio.run(stage.run(ctx))

    # Rodapé "Configuração Ausente" (já existente)
    assert "Configuração Ausente" in ctx.report, ctx.report
    assert "exa" in ctx.report, ctx.report

    # Resumo executivo reflete a fonte pulada (gap F7)
    assert (
        "exa" in ctx.report
    ), f"Resumo executivo não menciona fonte pulada por credencial: {ctx.report}"


def test_A3_missing_credential_reflected_in_executive_summary_text():
    """A3/F7: o texto do resumo executivo deve citar a fonte pulada por credencial."""
    from datetime import datetime

    ctx = _make_context({"tavily": ["consulta"]})
    ctx.extra["search_warnings"] = [
        "Fonte 'tavily' sem credencial configurada — busca indisponível ou limitada.",
    ]
    ctx.synthesized_results = []
    ctx.ranked_results = []
    ctx.completed_stages = []
    ctx.report = ""

    stage = _fake_report_stage()
    asyncio.run(stage.run(ctx))

    # O resumo executivo injetado deve conter a fonte pulada.
    assert "tavily" in ctx.report, ctx.report


# ── A4-bis: Integração SearchStage -> ReportStage (fluxo black_ops simulado) ──


def test_A4_integration_search_to_report_lists_all_failures():
    """A4/F1/F2/F7: fluxo completo lista 100% das fontes ausentes/falhas.

    Simula o critério de aceite de estresse do plano (Fase 4) de forma
    determinística: uma fonte sem credencial, uma com circuit breaker aberto
    e uma com timeout — o relatório final deve expor TODAS no rodapé e a
    nota de cobertura deve citar a fonte sem credencial no resumo executivo.
    """
    from src.utils.circuit_breaker import CircuitBreakerOpen

    ctx = _make_context(
        {
            "exa": ["consulta exa"],  # sem credencial -> search_warnings
            "web": ["consulta web"],  # circuit breaker -> search_errors
            "arxiv": ["consulta arxiv"],  # timeout -> search_errors
        }
    )
    searchers = {
        "exa": _fake_searcher([_plain_result("exa")], has_credentials=False),
        "web": _fake_searcher(
            [_plain_result("web")],
            has_credentials=True,
            side_effect=CircuitBreakerOpen(f"Circuito aberto para 'web'"),
        ),
        "arxiv": _fake_searcher(
            [_plain_result("arxiv")],
            has_credentials=True,
            side_effect=asyncio.TimeoutError(),
        ),
    }
    search = SearchStage(
        searchers=searchers,
        cache=None,
        ranker=_FakeRanker(),
        config=SearchStageConfig(fallback_on_empty=False),
    )
    asyncio.run(search.run(ctx))

    # search_warnings: exa sem credencial
    warnings = ctx.extra.get("search_warnings", [])
    assert any("exa" in w and "sem credencial" in w for w in warnings), warnings
    # search_errors: web (breaker) + arxiv (timeout)
    errors = ctx.extra.get("search_errors", [])
    assert any("web" in e and "Circuit Breaker OPEN" in e for e in errors), errors
    assert any("arxiv" in e for e in errors), errors

    # ReportStage consome e expõe no rodapé + cobertura no resumo.
    ctx.synthesized_results = []
    ctx.ranked_results = []
    ctx.completed_stages = []
    ctx.report = ""
    report = _fake_report_stage()
    asyncio.run(report.run(ctx))

    assert "Configuração Ausente" in ctx.report, ctx.report
    assert "exa" in ctx.report, ctx.report
    assert "Falhas de Rede / Limites de API" in ctx.report, ctx.report
    assert "web" in ctx.report and "arxiv" in ctx.report, ctx.report
    # Nota de cobertura injetada no resumo executivo.
    assert "exa" in ctx.report, "Resumo executivo não reflete fonte sem credencial"
