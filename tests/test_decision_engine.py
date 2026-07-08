"""
Testes para DynamicDecisionEngine — Validação da lógica de decisão dinâmica.

Testa:
- Decisão correta quando não há intent
- Decisão correta quando há resultados mas não ranqueamento
- Decisão correta quando confiança está baixa
- Decisão correta quando todos os estágios críticos estão completos
- Reset do histórico de decisões
"""

import pytest
from unittest.mock import MagicMock

from src.decision_engine import DynamicDecisionEngine, Decision
from src.pipeline.pipeline import PipelineContext
from src.types import IntentResult, Domain, Intention


@pytest.fixture
def decision_engine():
    """Cria um DynamicDecisionEngine para testes."""
    return DynamicDecisionEngine(
        confidence_threshold=50.0,
        max_iterations=10,
        operation_mode="cirurgia",
    )


@pytest.fixture
def mock_config():
    """Mock de configuração."""
    config = MagicMock()
    config.enable_dynamic_loop = True
    config.react_confidence_threshold = 50.0
    config.react_max_iterations = 10
    config.operation_mode = "cirurgia"
    return config


@pytest.mark.asyncio
async def test_decide_no_intent_returns_intent_stage(decision_engine):
    """Sem intent, deve retornar 'intent' como próxima etapa."""
    context = PipelineContext(query="test query")

    decision = decision_engine.decide(context)

    assert decision.next_stage == "intent"
    assert "intent" in decision.reason.lower()


@pytest.mark.asyncio
async def test_decide_has_intent_but_no_plan_returns_expand(decision_engine):
    """Com intent mas sem plano de fontes, deve retornar 'expand'."""
    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )
    # Mark intent as already executed
    decision_engine.mark_executed("intent")

    decision = decision_engine.decide(context)

    assert decision.next_stage == "expand"


@pytest.mark.asyncio
async def test_decide_has_results_no_rank_returns_rank(decision_engine):
    """Com resultados mas sem ranqueamento, deve retornar 'rank'."""
    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )
    context.source_plan = MagicMock()
    # Create a mock with a numeric score attribute
    mock1 = MagicMock()
    mock1.score = 0.8  # numeric score for ranking
    context.raw_results = [mock1]
    context.ranked_results = []  # Empty - rank not executed
    # Mark all prerequisite stages as executed
    decision_engine.mark_executed("intent")
    decision_engine.mark_executed("expand")
    decision_engine.mark_executed("search")
    # ranked_results is empty, rank not executed -> should return rank

    decision = decision_engine.decide(context)

    assert decision.next_stage == "rank"


@pytest.mark.asyncio
async def test_decide_low_confidence_returns_gap(decision_engine):
    """Com confiança baixa, deve retornar 'gap'."""
    decision_engine.confidence_threshold = 30.0

    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="media",
    )
    context.source_plan = MagicMock()
    # Create mocks with proper score attributes
    mock_raw = MagicMock()
    mock_raw.score = 10.0
    mock_ranked = MagicMock()
    mock_ranked.score = 10.0
    context.raw_results = [mock_raw]
    context.ranked_results = [mock_ranked]
    # Execute prerequisite stages
    decision_engine.mark_executed("intent")
    decision_engine.mark_executed("expand")
    decision_engine.mark_executed("search")
    decision_engine.mark_executed("rank")

    decision = decision_engine.decide(context)

    # Com confiança baixa, deve executar gap
    assert decision.next_stage == "gap"


@pytest.mark.asyncio
async def test_decide_complete_pipeline_returns_none(decision_engine):
    """Com todos os estágios críticos completos, deve retornar None (finalizar)."""
    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )
    context.source_plan = MagicMock()
    context.raw_results = []  # Empty to avoid code pattern check
    context.ranked_results = []
    context.report = "# Test Report\n\nContent here."

    # Marcar todos os estágios críticos como executados
    decision_engine._executed_stages = {
        "intent", "expand", "search", "rank", "verification",
        "graph_explorer", "gap", "synthesize", "report", "audit"
    }

    decision = decision_engine.decide(context)

    assert decision.next_stage is None
    assert "completa" in decision.reason.lower()


@pytest.mark.asyncio
async def test_decide_max_iterations_returns_none(decision_engine):
    """Ao atingir iteração máxima, deve forçar finalização."""
    decision_engine._iteration = decision_engine.max_iterations + 1
    decision_engine._executed_stages = {"intent", "expand", "search", "rank", "synthesize", "report"}

    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )

    decision = decision_engine.decide(context)

    assert decision.next_stage is None
    assert "máxima" in decision.reason.lower()


@pytest.mark.asyncio
async def test_mark_executed_updates_state(decision_engine):
    """mark_executed deve atualizar o conjunto de estágios executados."""
    decision_engine.mark_executed("test_stage")
    decision_engine.mark_executed("another_stage")

    assert "test_stage" in decision_engine._executed_stages
    assert "another_stage" in decision_engine._executed_stages


@pytest.mark.asyncio
async def test_reset_clears_state(decision_engine):
    """reset deve limpar o histórico de decisões."""
    decision_engine._executed_stages.add("intent")
    decision_engine._decision_history.append(Decision(next_stage="test", reason="test reason"))

    decision_engine.reset()

    assert len(decision_engine._executed_stages) == 0
    assert len(decision_engine._decision_history) == 0


@pytest.mark.asyncio
async def test_export_decision_trace(decision_engine):
    """export_decision_trace deve retornar estrutura correta."""
    decision_engine._executed_stages = {"intent", "expand"}
    decision_engine.mark_executed("intent")

    trace = decision_engine.export_decision_trace()

    assert "iterations" in trace
    assert "executed_stages" in trace
    assert "decisions" in trace
    assert "intent" in trace["executed_stages"]


@pytest.mark.asyncio
async def test_guerilha_mode_skips_non_critical(decision_engine):
    """Modo guerrilha deve pular etapas não-críticas."""
    decision_engine.operation_mode = "guerrilha"

    context = PipelineContext(query="test query")
    context.intent = IntentResult(
        domain=Domain.AI_ML,
        entities=["model"],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )
    context.source_plan = MagicMock()
    # Create proper mock results with good scores to ensure high confidence
    mock_raw = MagicMock()
    mock_raw.score = 80.0  # Good score for high confidence
    mock_ranked = MagicMock()
    mock_ranked.score = 80.0
    context.raw_results = [mock_raw]
    context.ranked_results = [mock_ranked]
    # Execute prerequisite stages
    decision_engine.mark_executed("intent")
    decision_engine.mark_executed("expand")
    decision_engine.mark_executed("search")
    decision_engine.mark_executed("rank")

    # Guerrilha mode deve ir direto para synthesize
    decision = decision_engine.decide(context)

    assert decision.next_stage == "synthesize"


@pytest.mark.asyncio
async def test_aggregate_confidence_calculation(decision_engine):
    """Testa o cálculo de confiança agregada."""
    context = PipelineContext(query="test query")

    # Sem resultados ranqueados
    confidence = decision_engine._aggregate_confidence(context)
    assert confidence == 0.0

    # Com resultados ranqueados
    mock_result = MagicMock()
    mock_result.score = 80.0
    context.ranked_results = [mock_result]
    confidence = decision_engine._aggregate_confidence(context)
    assert confidence == 80.0

    # Com claims verificadas
    context.extra["verified_claims"] = [
        {"status": "verified"},
        {"status": "verified"},
        {"status": "failed"},
    ]
    confidence = decision_engine._aggregate_confidence(context)
    # 2/3 verificadas * 20 + 80 base = ~93.3
    assert confidence > 80
