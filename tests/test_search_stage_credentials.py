"""TDD — FEAT-003 (Resiliência Bloco 3): Credencial-aware no SearchStage.

Cobre os critérios de aceitação 4.3.2 / edge cases 4.3.3:
  - Source no plano mas sem searcher registrado -> "sem searcher".
  - Searcher presente mas credencial ausente (web sem FIRECRAWL_API_KEY) -> "sem credencial".
  - Warnings expostos no rodapé do relatório Markdown.
  - Regressão: busca normal não é afetada quando há searcher + credencial.
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


def _fake_searcher(results: List[SearchResult], *, has_credentials: bool = True) -> Any:
    """Searcher falso determinístico para exercitar o SearchStage."""
    searcher = MagicMock()
    searcher.enabled = True
    searcher.has_credentials = has_credentials
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


# ── Testes TDD ────────────────────────────────────────────────────────────────


def test_source_in_plan_without_searcher_records_search_warning():
    """Edge 4.3.3 (Sem searcher): notion no plano, factory não registra -> warning."""
    ctx = _make_context({"notion": ["alguma consulta"]})
    searchers: dict[str, Any] = {}  # notion ausente de propósito
    stage = SearchStage(
        searchers=searchers,
        cache=None,
        ranker=_FakeRanker(),
        config=SearchStageConfig(fallback_on_empty=False),
    )

    asyncio.run(stage.run(ctx))

    warnings = ctx.extra.get("search_warnings", [])
    assert any("não tem searcher" in w and "notion" in w for w in warnings), warnings


def test_searcher_present_but_missing_credential_records_credential_warning():
    """Edge 4.3.3 (Sem credencial): web presente, FIRECRAWL_API_KEY ausente."""
    old = os.environ.pop("FIRECRAWL_API_KEY", None)
    try:
        ctx = _make_context({"web": ["busca qualquer"]})
        searchers = {
            "web": _fake_searcher([_plain_result("web")], has_credentials=False)
        }
        stage = SearchStage(
            searchers=searchers,
            cache=None,
            ranker=_FakeRanker(),
            config=SearchStageConfig(fallback_on_empty=False),
        )

        asyncio.run(stage.run(ctx))

        warnings = ctx.extra.get("search_warnings", [])
        assert any("sem credencial" in w and "web" in w for w in warnings), warnings
    finally:
        if old is not None:
            os.environ["FIRECRAWL_API_KEY"] = old


def test_search_warnings_exposed_in_report_footer():
    """Edge 4.3.2 (rodapé): report_stage expõe search_warnings no Markdown."""
    ctx = _make_context({"notion": ["consulta"]})
    ctx.extra["search_warnings"] = [
        "Fonte 'notion' no plano não tem searcher registrado (sem credencial/config).",
    ]
    ctx.synthesized_results = []
    ctx.ranked_results = []
    ctx.completed_stages = []
    ctx.report = ""

    stage = ReportStage()
    asyncio.run(stage.run(ctx))

    assert "não tem searcher" in ctx.report or "sem searcher" in ctx.report, ctx.report
    assert "notion" in ctx.report


def test_normal_search_unaffected_when_searcher_and_credentials_present():
    """Regressão: searcher presente + credencial -> busca normal, sem warnings."""
    old = os.environ.pop("FIRECRAWL_API_KEY", None)  # pragma: allowlist secret
    os.environ["FIRECRAWL_API_KEY"] = "dummy-key-for-test"  # pragma: allowlist secret
    try:
        ctx = _make_context({"web": ["busca normal"]})
        searchers = {
            "web": _fake_searcher([_plain_result("web")], has_credentials=True)
        }
        stage = SearchStage(
            searchers=searchers,
            cache=None,
            ranker=_FakeRanker(),
            config=SearchStageConfig(fallback_on_empty=False),
        )

        asyncio.run(stage.run(ctx))

        warnings = ctx.extra.get("search_warnings", [])
        assert warnings == [], warnings
        assert len(ctx.ranked_results) == 1
    finally:
        os.environ.pop("FIRECRAWL_API_KEY", None)
        if old is not None:
            os.environ["FIRECRAWL_API_KEY"] = old
