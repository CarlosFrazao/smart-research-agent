"""Onda 1 / M1.1 — Relevance floor (piso de relevância).

Descarta resultados off-topic (sem sobreposição de tokens com a query
original) que entram quando a expansão de query degrada — ex.: SearXNG
trazendo "Agenda Cultural de Manaus" numa pesquisa sobre MVCC.

Regras:
- Fontes estruturadas confiáveis (arxiv/github) NÃO são filtradas.
- Nunca deixa a coleta abaixo de ``relevance_floor_min_keep`` (degradação suave).

Ver: Plan_SRA_Melhorias_Qualidade_2026-07-16.md
"""

from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig
from src.types import SearchResult


def _stage(**cfg) -> SearchStage:
    return SearchStage(
        searchers={},
        cache=None,
        ranker=None,
        config=SearchStageConfig(**cfg),
    )


QUERY = "concurrency bugs MVCC lock-free embedded databases KuzuDB DuckDB"


def _r(source, title, desc=""):
    return SearchResult(source=source, title=title, url="https://x.com", description=desc)


def test_offtopic_searxng_result_is_dropped():
    stage = _stage(relevance_floor_min_keep=1)
    results = [
        _r("searxng", "Agenda Cultural de Manaus", "shows e eventos ao vivo"),
        _r("searxng", "MVCC concurrency in embedded databases", "lock-free KuzuDB"),
        _r("searxng", "Ingressos para shows e teatros", "eventos"),
        _r("searxng", "DuckDB deadlock and starvation analysis", "bugs concurrency"),
    ]
    kept = stage._apply_relevance_floor(results, QUERY)
    titles = [r.title for r in kept]
    assert "Agenda Cultural de Manaus" not in titles
    assert "Ingressos para shows e teatros" not in titles
    assert any("MVCC" in t for t in titles)
    assert any("DuckDB" in t for t in titles)


def test_trusted_sources_are_never_filtered():
    stage = _stage(relevance_floor_min_keep=1)
    results = [
        _r("arxiv", "Totally unrelated paper about penguins", "biology"),
        _r("github", "unrelated/repo", "cooking recipes"),
    ]
    kept = stage._apply_relevance_floor(results, QUERY)
    # arxiv/github são consultas dirigidas — isentos do piso.
    assert len(kept) == 2


def test_graceful_degradation_keeps_minimum():
    """Se tudo for off-topic, mantém pelo menos min_keep resultados."""
    stage = _stage(relevance_floor_min_keep=3)
    results = [_r("searxng", f"unrelated topic {i}", "noise") for i in range(6)]
    kept = stage._apply_relevance_floor(results, QUERY)
    assert len(kept) == 3


def test_empty_query_does_not_filter():
    stage = _stage()
    results = [_r("searxng", "anything", "noise")]
    kept = stage._apply_relevance_floor(results, "")
    assert len(kept) == 1


def test_relevant_results_all_kept():
    stage = _stage(relevance_floor_min_keep=1)
    results = [
        _r("searxng", "MVCC concurrency KuzuDB", "lock-free databases"),
        _r("searxng", "DuckDB deadlock bugs", "embedded databases starvation"),
    ]
    kept = stage._apply_relevance_floor(results, QUERY)
    assert len(kept) == 2
