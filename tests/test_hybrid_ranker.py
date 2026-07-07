import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ranker import QualityRanker
from src.ranking.hybrid_ranker import BM25, HybridRanker, _normalize, _tokenize
from src.types import SearchResult


def make_result(source, title, description="", metrics=None, url=None):
    return SearchResult(
        source=source,
        title=title,
        url=url or f"https://{source}.com/{title}",
        description=description,
        metrics=metrics or {},
    )


class FakeSemanticScorer:
    """Simula o SemanticReranker sem carregar nenhum modelo real."""

    def __init__(self, score_map):
        self.score_map = score_map
        self.calls = 0

    async def rerank(self, query, results, top_k=None):
        self.calls += 1
        out = []
        for r in results:
            item = dict(r)
            item["_semantic_score"] = self.score_map.get(r["title"])
            out.append(item)
        return out


# ── BM25 puro ────────────────────────────────────────────────────────────


def test_bm25_scores_relevant_doc_higher():
    docs = [
        _tokenize("langgraph multi agent orchestration framework"),
        _tokenize("recipe for chocolate cake with frosting"),
    ]
    bm25 = BM25(docs)
    scores = bm25.scores_for_query(_tokenize("langgraph agent framework"))
    assert scores[0] > scores[1]


def test_normalize_handles_constant_values():
    assert _normalize([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]
    assert _normalize([]) == []


# ── HybridRanker ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hybrid_ranker_without_semantic_scorer_still_works():
    results = [
        make_result("github", "langgraph orchestration framework"),
        make_result("github", "unrelated cooking blog"),
    ]
    hybrid = HybridRanker(semantic_scorer=None)
    scored = await hybrid.rank(
        results, query="langgraph orchestration", heuristic_scores=[50.0, 50.0]
    )
    assert len(scored) == 2
    assert scored[0].result.title == "langgraph orchestration framework"
    assert scored[0].embedding_score is None


@pytest.mark.asyncio
async def test_hybrid_ranker_uses_semantic_scorer_when_available():
    results = [
        make_result("github", "topic A"),
        make_result("github", "topic B"),
    ]
    scorer = FakeSemanticScorer({"topic A": 0.9, "topic B": 0.1})
    hybrid = HybridRanker(semantic_scorer=scorer, pre_filter_top_n=10)
    scored = await hybrid.rank(
        results, query="topic", heuristic_scores=[50.0, 50.0]
    )
    by_title = {s.result.title: s for s in scored}
    assert by_title["topic A"].embedding_score == 0.9
    assert by_title["topic A"].final_score > by_title["topic B"].final_score


@pytest.mark.asyncio
async def test_hybrid_ranker_pre_filter_limits_embedding_calls():
    results = [make_result("github", f"item {i} keyword") for i in range(20)]
    scorer = FakeSemanticScorer({f"item {i} keyword": 0.5 for i in range(20)})
    hybrid = HybridRanker(semantic_scorer=scorer, pre_filter_top_n=5)
    heuristic_scores = [float(i) for i in range(20)]  # item 19 = melhor heuristica
    scored = await hybrid.rank(results, query="keyword", heuristic_scores=heuristic_scores)
    assert len(scored) == 20  # nenhum resultado descartado
    embedded = [s for s in scored if s.embedding_score is not None]
    assert len(embedded) <= 5  # so o pre-filtro passou pelo scorer


@pytest.mark.asyncio
async def test_hybrid_ranker_survives_semantic_scorer_failure():
    results = [make_result("github", "a"), make_result("github", "b")]

    class BrokenScorer:
        async def rerank(self, query, results, top_k=None):
            raise RuntimeError("modelo indisponivel")

    hybrid = HybridRanker(semantic_scorer=BrokenScorer())
    scored = await hybrid.rank(results, query="a", heuristic_scores=[50.0, 50.0])
    assert len(scored) == 2  # nao quebra, so ignora o sinal de embedding


def test_hybrid_ranker_rejects_mismatched_lengths():
    hybrid = HybridRanker()
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(
            hybrid.rank(
                [make_result("github", "a")], query="a", heuristic_scores=[1.0, 2.0]
            )
        )


# ── QualityRanker (fachada) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quality_ranker_without_query_unchanged_behavior():
    ranker = QualityRanker()
    results = [make_result("github", "repo", metrics={"stars": 100})]
    ranked = await ranker.rank(results)
    assert ranked[0].score_breakdown["base_score"] == ranked[0].score
    # bm25_score e embedding_score agora SEMPRE existem no breakdown (com
    # valor None quando nao ha query) — garante schema estavel para quem
    # consome RankedResult a jusante (ConfidenceScorerV2, ConflictDetector).
    assert "bm25_score" in ranked[0].score_breakdown
    assert ranked[0].score_breakdown["bm25_score"] is None
    assert "embedding_score" in ranked[0].score_breakdown
    assert ranked[0].score_breakdown["embedding_score"] is None


@pytest.mark.asyncio
async def test_quality_ranker_with_query_uses_hybrid_ranking():
    scorer = FakeSemanticScorer({"very relevant langgraph guide": 0.95, "unrelated topic": 0.05})
    ranker = QualityRanker(semantic_scorer=scorer)
    results = [
        make_result("github", "unrelated topic", metrics={"stars": 1000}),
        make_result("github", "very relevant langgraph guide", metrics={"stars": 10}),
    ]
    ranked = await ranker.rank(results, query="langgraph guide")
    assert "bm25_score" in ranked[0].score_breakdown
    assert "embedding_score" in ranked[0].score_breakdown
    # o resultado mais relevante deve vencer mesmo tendo menos estrelas
    assert ranked[0].title == "very relevant langgraph guide"


@pytest.mark.asyncio
async def test_quality_ranker_hybrid_failure_falls_back_gracefully():
    class BrokenScorer:
        async def rerank(self, query, results, top_k=None):
            raise RuntimeError("boom")

    ranker = QualityRanker(semantic_scorer=BrokenScorer())
    results = [make_result("github", "repo", metrics={"stars": 5})]
    ranked = await ranker.rank(results, query="repo")
    assert len(ranked) == 1


@pytest.mark.asyncio
async def test_quality_ranker_empty_results():
    ranker = QualityRanker()
    assert await ranker.rank([]) == []
    assert await ranker.rank([], query="x") == []
