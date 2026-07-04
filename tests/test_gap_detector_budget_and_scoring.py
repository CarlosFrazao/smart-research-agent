import pytest
from unittest.mock import AsyncMock, MagicMock
from src.gap_detector import GapDetector, GapDetectionState
from src.types import RankedResult, IntentResult, Domain, Intention


def make_results(n, sources=None):
    sources = sources or ["github", "reddit", "hackernews", "arxiv"]
    return [
        RankedResult(
            source=sources[i % len(sources)],
            title=f"project{i}",
            url=f"https://x.com/{i}",
            description="d",
            score=50.0,
        )
        for i in range(n)
    ]


@pytest.fixture
def intent():
    return IntentResult(
        domain=Domain.SAAS_B2B,
        entities=["CRM"],
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )


@pytest.mark.asyncio
async def test_dedicated_budget_abort(intent):
    llm = MagicMock()
    llm.generate_structured = AsyncMock(side_effect=AssertionError("nao deveria chamar LLM"))
    detector = GapDetector(llm, max_budget_usd=0.10)
    state = GapDetectionState(accumulated_cost_usd=0.50)
    gap = await detector.detect(make_results(20), "crm", intent, state=state)
    assert gap.is_complete is True
    assert "orcamento" in gap.rationale.lower() or "orçamento" in gap.rationale.lower()
    llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_diminishing_returns_abort(intent):
    llm = MagicMock()
    llm.generate_structured = AsyncMock(side_effect=AssertionError("nao deveria chamar LLM"))
    detector = GapDetector(llm, min_new_results_ratio=0.05)
    state = GapDetectionState(previous_result_count=100, iterations_run=1)
    # 101 resultados = apenas 1% de crescimento -> abaixo do limiar de 5%
    gap = await detector.detect(make_results(101), "crm", intent, state=state)
    assert gap.is_complete is True
    assert "decrescente" in gap.rationale.lower()


@pytest.mark.asyncio
async def test_gap_scoring_prioritizes_and_trims(intent):
    llm = MagicMock()
    llm.generate_structured = AsyncMock(
        return_value={
            "is_complete": False,
            "missing_aspects": ["a", "b"],
            "new_queries": [
                "totalmente irrelevante xyz",
                "crm open source alternative comparison review",
                "crm",
                "algo aleatorio sem relacao nenhuma",
                "crm enterprise features 2026",
            ],
            "confidence": "alta",
            "rationale": "teste",
        }
    )
    detector = GapDetector(llm, max_new_queries=2)
    results = make_results(25)
    gap = await detector.detect(results, "crm software", intent)
    assert len(gap.new_queries) == 2
    # As queries mais relacionadas ao termo original devem vencer as irrelevantes
    assert "crm open source alternative comparison review" in gap.new_queries


@pytest.mark.asyncio
async def test_state_persists_across_calls(intent):
    llm = MagicMock()
    llm.generate_structured = AsyncMock(
        return_value={
            "is_complete": False,
            "missing_aspects": [],
            "new_queries": ["q"],
            "confidence": "media",
            "rationale": "r",
        }
    )
    detector = GapDetector(llm)
    state = GapDetectionState()
    await detector.detect(make_results(20), "crm", intent, state=state)
    assert state.iterations_run == 1
    assert state.previous_result_count == 20
