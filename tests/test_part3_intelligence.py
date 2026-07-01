import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from src.types import (
    Domain, Intention, IntentResult, ExpandedQuery, SearchResult, RankedResult,
)


# ─── Intent Analyzer ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intent_analyzer_heuristic_saas(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("best open source CRM like HubSpot")
    assert result.domain == Domain.SAAS_B2B
    assert result.confidence == "media"


@pytest.mark.asyncio
async def test_intent_analyzer_heuristic_ai_ml(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("best local LLM model for coding")
    assert result.domain == Domain.AI_ML


@pytest.mark.asyncio
async def test_intent_analyzer_compare_intention(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("n8n vs Zapier vs Make")
    assert result.intention == Intention.COMPARE


@pytest.mark.asyncio
async def test_intent_analyzer_discover_default(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("CRM open source")
    assert result.intention == Intention.DISCOVER


@pytest.mark.asyncio
async def test_intent_analyzer_urgency_sim(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("best AI tools 2026")
    assert result.urgency == "sim"


@pytest.mark.asyncio
async def test_intent_analyzer_uses_llm(mock_llm_client):
    mock_llm_client.generate_structured = AsyncMock(return_value={
        "domain": "ai_ml",
        "entities": ["GPT", "Claude"],
        "intention": "compare",
        "urgency": "nao",
        "confidence": "alta",
    })
    from src.intent_analyzer import IntentAnalyzer
    analyzer = IntentAnalyzer(mock_llm_client)
    result = await analyzer.analyze("GPT vs Claude")
    assert result.domain == Domain.AI_ML
    assert result.confidence == "alta"
    assert "GPT" in result.entities


# ─── Query Expander ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_expander_fallback(mock_llm_client, sample_intent):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.query_expander import QueryExpander
    expander = QueryExpander(mock_llm_client)
    results = await expander.expand("crm open source", sample_intent)
    assert len(results) >= 4
    assert all(isinstance(q, ExpandedQuery) for q in results)


@pytest.mark.asyncio
async def test_query_expander_llm_result(mock_llm_client, sample_intent):
    mock_llm_client.generate_structured = AsyncMock(return_value={
        "queries": [
            {"query": f"open source crm {i}", "type": "qualificador", "priority": "alta", "rationale": "test"}
            for i in range(10)
        ]
    })
    from src.query_expander import QueryExpander
    expander = QueryExpander(mock_llm_client)
    results = await expander.expand("crm", sample_intent)
    assert len(results) == 10
    assert all(q.type == "qualificador" for q in results)


@pytest.mark.asyncio
async def test_query_expander_fallback_has_original(mock_llm_client, sample_intent):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM offline"))
    from src.query_expander import QueryExpander
    expander = QueryExpander(mock_llm_client)
    results = await expander.expand("crm open source", sample_intent)
    queries = [q.query for q in results]
    assert "crm open source" in queries


# ─── Source Planner ───────────────────────────────────────────────────────────

def test_source_planner_saas_b2b(sample_intent, sample_expanded_query):
    from src.source_planner import SourcePlanner
    planner = SourcePlanner()
    plan = planner.plan(sample_intent, [sample_expanded_query])
    assert "github" in plan.primary
    assert "producthunt" in plan.primary
    assert isinstance(plan.sources, dict)
    assert len(plan.primary) > 0


def test_source_planner_ai_ml():
    from src.source_planner import SourcePlanner
    from src.types import IntentResult, Domain, Intention, ExpandedQuery
    intent = IntentResult(
        domain=Domain.AI_ML, entities=[], intention=Intention.DISCOVER,
        urgency="nao", confidence="alta"
    )
    query = ExpandedQuery(query="llm local", type="qualificador", priority="alta", rationale="test")
    planner = SourcePlanner()
    plan = planner.plan(intent, [query])
    assert "arxiv" in plan.primary
    assert "github" in plan.primary


def test_source_planner_all_sources_have_queries(sample_intent, sample_expanded_query):
    from src.source_planner import SourcePlanner
    queries = [
        ExpandedQuery(query=f"query {i}", type="qualificador", priority="alta", rationale="test")
        for i in range(5)
    ]
    planner = SourcePlanner()
    plan = planner.plan(sample_intent, queries)
    # Todas as fontes no plano devem ter pelo menos uma query
    for source, source_queries in plan.sources.items():
        assert len(source_queries) >= 1, f"{source} sem queries"


# ─── Quality Ranker ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ranker_github_score(sample_search_result):
    from src.ranker import QualityRanker
    ranker = QualityRanker()
    sample_search_result.metrics["updated_at"] = "2026-01-01T00:00:00Z"
    results = await ranker.rank([sample_search_result])
    assert len(results) == 1
    assert 0 <= results[0].score <= 100
    assert results[0].source == "github"


@pytest.mark.asyncio
async def test_ranker_reddit_score():
    from src.ranker import QualityRanker
    from src.types import SearchResult
    ranker = QualityRanker()
    result = SearchResult(
        source="reddit",
        title="Best CRM 2026",
        url="https://reddit.com/r/selfhosted",
        description="Discussion",
        metrics={"upvotes": 1000, "comments": 100, "subreddit_relevance": 25, "created_at": "2026-01-01"},
    )
    ranked = await ranker.rank([result])
    assert 0 <= ranked[0].score <= 100


@pytest.mark.asyncio
async def test_ranker_hn_score():
    from src.ranker import QualityRanker
    from src.types import SearchResult
    ranker = QualityRanker()
    result = SearchResult(
        source="hackernews",
        title="Show HN: Twenty CRM",
        url="https://twenty.com",
        description="",
        metrics={"points": 500, "comments": 200, "created_at": "2026-02-01T00:00:00Z", "url": "https://twenty.com"},
    )
    ranked = await ranker.rank([result])
    assert ranked[0].score > 50


@pytest.mark.asyncio
async def test_ranker_sorted_by_score():
    from src.ranker import QualityRanker
    from src.types import SearchResult
    ranker = QualityRanker()
    r1 = SearchResult(source="github", title="low/stars", url="https://github.com/low", description="", metrics={"stars": 10, "forks": 1})
    r2 = SearchResult(source="github", title="high/stars", url="https://github.com/high", description="", metrics={"stars": 50000, "forks": 5000})
    ranked = await ranker.rank([r1, r2])
    assert ranked[0].title == "high/stars"
    assert ranked[0].score > ranked[1].score


@pytest.mark.asyncio
async def test_ranker_generic_score():
    from src.ranker import QualityRanker
    from src.types import SearchResult
    ranker = QualityRanker()
    result = SearchResult(source="arxiv", title="Paper", url="https://arxiv.org/1234", description="Research")
    ranked = await ranker.rank([result])
    assert ranked[0].score == 50.0


# ─── Gap Detector ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gap_detector_few_results(mock_llm_client, sample_intent):
    from src.gap_detector import GapDetector
    from src.types import RankedResult
    detector = GapDetector(mock_llm_client)
    results = []
    gap = await detector.detect(results, "crm open source", sample_intent)
    assert gap.is_complete is False
    assert "poucos resultados" in gap.missing_aspects


@pytest.mark.asyncio
async def test_gap_detector_few_sources(mock_llm_client, sample_intent):
    from src.gap_detector import GapDetector
    from src.types import RankedResult
    from datetime import datetime
    detector = GapDetector(mock_llm_client)
    results = [
        RankedResult(
            source="github", title=f"repo{i}", url=f"https://github.com/r{i}",
            description="desc", score=50.0,
        )
        for i in range(15)
    ]
    gap = await detector.detect(results, "crm", sample_intent)
    assert gap.is_complete is False


@pytest.mark.asyncio
async def test_gap_detector_complete_via_llm(mock_llm_client, sample_intent):
    mock_llm_client.generate_structured = AsyncMock(return_value={
        "is_complete": True,
        "missing_aspects": [],
        "new_queries": [],
        "confidence": "alta",
        "rationale": "Pesquisa completa",
    })
    from src.gap_detector import GapDetector
    from src.types import RankedResult
    detector = GapDetector(mock_llm_client)
    results = [
        RankedResult(
            source=src, title=f"project{i}", url=f"https://{src}.com/p{i}",
            description="desc", score=60.0,
        )
        for i, src in enumerate(["github", "reddit", "hackernews", "arxiv"] * 5)
    ]
    gap = await detector.detect(results, "crm", sample_intent)
    assert gap.is_complete is True


@pytest.mark.asyncio
async def test_gap_detector_llm_fallback(mock_llm_client, sample_intent):
    mock_llm_client.generate_structured = AsyncMock(side_effect=Exception("LLM timeout"))
    from src.gap_detector import GapDetector
    from src.types import RankedResult
    detector = GapDetector(mock_llm_client)
    results = [
        RankedResult(
            source=src, title=f"project{i}", url=f"https://{src}.com/p{i}",
            description="desc", score=60.0,
        )
        for i, src in enumerate(["github", "reddit", "hackernews", "awesome"] * 5)
    ]
    gap = await detector.detect(results, "crm", sample_intent)
    assert gap.is_complete is True  # fallback retorna True


# ─── Synthesizer ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesizer_deduplicates(sample_ranked_result):
    from src.synthesizer import Synthesizer
    synth = Synthesizer()
    results = [sample_ranked_result, sample_ranked_result]  # Mesmo resultado duplicado
    synthesized = await synth.synthesize(results)
    assert len(synthesized) == 1


@pytest.mark.asyncio
async def test_synthesizer_clusters_by_entity():
    from src.synthesizer import Synthesizer
    from src.types import RankedResult
    synth = Synthesizer()
    r1 = RankedResult(source="github", title="twenty/twenty", url="https://github.com/t/t", description="CRM", score=80.0)
    r2 = RankedResult(source="reddit", title="twenty crm review", url="https://reddit.com/t/t", description="Review", score=60.0)
    r3 = RankedResult(source="github", title="supabase/supabase", url="https://github.com/s/s", description="DB", score=90.0)
    synthesized = await synth.synthesize([r1, r2, r3])
    assert len(synthesized) >= 1


@pytest.mark.asyncio
async def test_synthesizer_highlights_stars():
    from src.synthesizer import Synthesizer
    from src.types import RankedResult
    synth = Synthesizer()
    r = RankedResult(
        source="github", title="popular/repo", url="https://github.com/p/r",
        description="Popular repo", score=95.0,
        metrics={"stars": 50000, "forks": 3000},
    )
    synthesized = await synth.synthesize([r])
    assert len(synthesized) == 1
    assert any("stars" in h for h in synthesized[0].highlights)


@pytest.mark.asyncio
async def test_synthesizer_applies_source_cap():
    from src.synthesizer import Synthesizer
    from src.types import RankedResult
    synth = Synthesizer()
    results = [
        RankedResult(
            source="github", title=f"repo{i}/project{i}", url=f"https://github.com/{i}/{i}",
            description=f"Project {i}", score=float(100 - i),
        )
        for i in range(10)
    ]
    synthesized = await synth.synthesize(results)
    github_count = sum(1 for r in synthesized if "github" in r.sources)
    assert github_count <= 3
