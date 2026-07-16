"""Regressão do BUG crítico — starvation de fontes por teto de volume.

Antes da correção, uma fonte rápida e prolífica (ex.: arXiv) enchia o teto
global de 50 resultados e cancelava as demais fontes pendentes (ex.: github),
mesmo quando o usuário pedia explicitamente aquela fonte. A correção adiciona
justiça entre fontes: o teto de volume só cancela pendentes DEPOIS que cada
fonte distinta do plano tiver sido consultada ao menos uma vez.

Ver: sessão 2026-07-16 (teste de estresse black_ops).
"""

import asyncio

import pytest

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig
from src.types import (
    Domain,
    Intention,
    IntentResult,
    ExpandedQuery,
    SourcePlan,
    SearchResult,
    RankedResult,
)
from src.utils.circuit_breaker import CircuitBreakerRegistry


class MockSearcher:
    def __init__(self, name, results, delay=0.0):
        self.name = name
        self.results = results
        self.delay = delay
        self.calls = 0
        self.enabled = True

    async def search(self, query, domain=None):
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.results


class MockCache:
    def __init__(self):
        self.store = {}

    async def get(self, namespace, key):
        return None

    async def set(self, namespace, key, value, ttl=None):
        self.store[f"{namespace}:{key}"] = value


class MockRanker:
    async def rank(self, results):
        out = []
        for r in results:
            out.append(
                RankedResult(
                    source=r.source,
                    title=r.title,
                    url=r.url,
                    description=r.description,
                    metrics=r.metrics,
                    raw=r.raw,
                    fetched_at=r.fetched_at,
                    confidence_score=r.confidence_score,
                    evidence_quality=r.evidence_quality,
                    citations=r.citations,
                    contradictions=r.contradictions,
                    hallucination_flags=r.hallucination_flags,
                    score=50.0,
                )
            )
        return out


def _make_results(source, n):
    return [
        SearchResult(
            source=source,
            title=f"{source} result {i}",
            url=f"https://{source}.com/{i}",
            description="desc",
            confidence_score=0.3,  # abaixo do threshold de early termination
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_prolific_source_does_not_starve_others():
    """arXiv rápido com 60 resultados NÃO deve impedir github de rodar."""
    CircuitBreakerRegistry.reset_all()

    # arxiv: rápido e prolífico (enche o teto de 50 sozinho)
    arxiv = MockSearcher("arxiv", _make_results("arxiv", 60), delay=0.0)
    # github: mais lento, poucos resultados — a fonte que o usuário quer
    github = MockSearcher("github", _make_results("github", 3), delay=0.05)

    searchers = {"arxiv": arxiv, "github": github}
    stage = SearchStage(
        searchers,
        MockCache(),
        MockRanker(),
        config=SearchStageConfig(early_termination_enabled=False),
    )

    intent = IntentResult(
        domain=Domain.DEV_TOOLS,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )
    expanded = [ExpandedQuery(query="concurrency mvcc databases", type="original")]
    source_plan = SourcePlan(
        sources={"arxiv": expanded, "github": expanded},
        primary=["arxiv", "github"],
        secondary=[],
    )
    context = PipelineContext(query="concurrency mvcc databases")
    context.intent = intent
    context.expanded_queries = expanded
    context.source_plan = source_plan

    await stage.run(context)

    # A fonte github DEVE ter sido consultada apesar do teto de volume.
    assert github.calls == 1, "github foi cancelado pelo teto de volume (starvation)"
    # E seus resultados devem aparecer.
    sources_seen = {r.source for r in context.raw_results}
    assert "github" in sources_seen
    assert "arxiv" in sources_seen


@pytest.mark.asyncio
async def test_fairness_disabled_allows_early_cut():
    """Com enforce_source_fairness=False, o teto volta a cortar cedo."""
    CircuitBreakerRegistry.reset_all()

    arxiv = MockSearcher("arxiv", _make_results("arxiv", 60), delay=0.0)
    github = MockSearcher("github", _make_results("github", 3), delay=0.2)

    searchers = {"arxiv": arxiv, "github": github}
    stage = SearchStage(
        searchers,
        MockCache(),
        MockRanker(),
        config=SearchStageConfig(
            early_termination_enabled=False,
            enforce_source_fairness=False,
        ),
    )
    intent = IntentResult(
        domain=Domain.DEV_TOOLS,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )
    expanded = [ExpandedQuery(query="concurrency mvcc databases", type="original")]
    source_plan = SourcePlan(
        sources={"arxiv": expanded, "github": expanded},
        primary=["arxiv", "github"],
        secondary=[],
    )
    context = PipelineContext(query="concurrency mvcc databases")
    context.intent = intent
    context.expanded_queries = expanded
    context.source_plan = source_plan

    await stage.run(context)

    # arxiv encheu o teto; github (lento) pode ter sido cancelado.
    assert len(context.raw_results) >= 50
