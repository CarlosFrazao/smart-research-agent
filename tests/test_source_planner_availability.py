"""Onda 2 / M2.2 — filtragem de fontes indisponíveis no SourcePlanner.

Antes da correção, o plano incluía fontes que não têm searcher instanciado
(ex.: serpapi sem lib, sharepoint sem credencial), gerando WARNINGs repetidos
"no registered searcher" no SearchStage. Agora o planner filtra essas fontes
fantasma quando ``available_sources`` é injetado.

Ver: Plan_SRA_Melhorias_Qualidade_2026-07-16.md
"""

from src.source_planner import SourcePlanner
from src.types import Domain, ExpandedQuery, Intention, IntentResult


def _intent() -> IntentResult:
    return IntentResult(
        domain=Domain.INFRASTRUCTURE,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )


def _queries() -> list[ExpandedQuery]:
    return [ExpandedQuery(query="concurrency mvcc databases", type="original")]


AVAILABLE = {
    "web",
    "arxiv",
    "github",
    "stackoverflow",
    "hackernews",
    "reddit",
    "searxng",
}


def test_phantom_sources_are_filtered_when_available_set_injected():
    p = SourcePlanner(mode="black_ops")
    p.available_sources = AVAILABLE
    plan = p.plan(_intent(), _queries())
    assert "serpapi" not in plan.sources
    assert "sharepoint" not in plan.sources
    # Fontes reais permanecem.
    assert "arxiv" in plan.sources
    assert "github" in plan.sources


def test_no_filtering_when_available_not_injected():
    """Retrocompatível: sem available_sources, nada é filtrado."""
    p = SourcePlanner(mode="black_ops")
    plan = p.plan(_intent(), _queries())
    assert "serpapi" in plan.sources


def test_primary_and_secondary_are_also_filtered():
    p = SourcePlanner(mode="black_ops")
    p.available_sources = AVAILABLE
    plan = p.plan(_intent(), _queries())
    assert all(s in AVAILABLE for s in plan.primary)
    assert all(s in AVAILABLE for s in plan.secondary)


def test_empty_filter_result_falls_back_to_original():
    """Se todas as fontes forem indisponíveis, mantém o plano original."""
    p = SourcePlanner(mode="black_ops")
    p.available_sources = {"nonexistent_source"}
    plan = p.plan(_intent(), _queries())
    # Degradação suave: não zera a busca.
    assert len(plan.sources) > 0
