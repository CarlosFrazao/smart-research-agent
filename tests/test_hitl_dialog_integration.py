"""tests/test_hitl_dialog_integration.py — Testes de integração do HITLDialogAgent no Orquestrador, SynthesizeStage e API FastAPI.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import Config
from src.orchestrator import Orchestrator
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.synthesize_stage import SynthesizeStage
from src.hitl_dialog_agent import HITLDialogAgent, DialogDecision, DialogTurn, DialogType
from src.types import RankedResult, SearchResult, ExpandedQuery


@pytest.mark.asyncio
async def test_orchestrator_hitl_dialog_initialization():
    config = Config()
    with patch("src.pipeline.stage_factory.StageFactory.initialize_components"), \
         patch("src.pipeline.stage_factory.StageFactory.build_pipeline"), \
         patch("src.orchestrator.FallbackManager"):

        orchestrator = Orchestrator(config=config)
        assert orchestrator.hitl_dialog is not None
        assert isinstance(orchestrator.hitl_dialog, HITLDialogAgent)


@pytest.mark.asyncio
async def test_apply_hitl_decision():
    config = Config()
    with patch("src.pipeline.stage_factory.StageFactory.initialize_components"), \
         patch("src.pipeline.stage_factory.StageFactory.build_pipeline"), \
         patch("src.orchestrator.FallbackManager"):

        orchestrator = Orchestrator(config=config)
        context = PipelineContext(query="original query")

        # Teste 1: Ação "pivot_to_contradiction" com additional_query
        decision_pivot = {
            "action": "pivot_to_contradiction",
            "parameters": {"additional_query": "nova query investigacao"},
        }
        await orchestrator._apply_hitl_decision(decision_pivot, context)
        assert len(context.expanded_queries) == 1
        assert context.expanded_queries[0].query == "nova query investigacao"
        assert context.expanded_queries[0].type == "hitl_pivot"

        # Teste 2: Ação "expand_scope" (não levanta erro e executa)
        decision_expand = {
            "action": "expand_scope",
            "parameters": {"new_queries": ["query b"]},
        }
        await orchestrator._apply_hitl_decision(decision_expand, context)
        # Não adiciona query pois action é expand_scope (apenas loga)
        assert len(context.expanded_queries) == 1


@pytest.mark.asyncio
async def test_synthesize_stage_finding_generation():
    # Setup context com resultados e mocks de detectores
    context = PipelineContext(query="minha query")
    context.intent = MagicMock()
    context.session_id = "session_test"

    # Ranked results com domínios REALMENTE presentes no denylist do
    # MisinformationDetector (src/misinformation_detector.py). URLs fictícias
    # não são sinalizadas — usamos domínios reais para que os 2 findings de
    # 'suspicious_source' de fato sejam gerados.
    results = [
        RankedResult(
            title="Claim divergente 1",
            url="http://infowars.com/1",
            snippet="O valor cresceu 15%",
            source="infowars",
            score=0.9,
            relevance_score=0.9,
            confidence_score=0.8,
        ),
        RankedResult(
            title="Claim divergente 2",
            url="http://naturalnews.com/2",
            snippet="O valor cresceu 90%",
            source="naturalnews",
            score=0.8,
            relevance_score=0.8,
            confidence_score=0.8,
        )
    ]
    context.ranked_results = results

    orchestrator_mock = MagicMock()
    context.extras["orchestrator"] = orchestrator_mock

    # Mock do ConflictDetector
    mock_conflict_report = MagicMock()
    mock_conflict = MagicMock()
    mock_conflict.metric_name = "crescimento"
    mock_conflict.severity = "critical"
    # Claims
    claim1 = MagicMock()
    claim1.context = "cresceu 15%"
    claim1.value = 15.0
    claim1.unit = "%"
    claim1.source_name = "fonte 1"
    claim2 = MagicMock()
    claim2.context = "cresceu 90%"
    claim2.value = 90.0
    claim2.unit = "%"
    claim2.source_name = "fonte 2"
    mock_conflict.claims = [claim1, claim2]
    mock_conflict_report.conflicts = [mock_conflict]

    orchestrator_mock.conflict_detector = MagicMock()
    orchestrator_mock.conflict_detector.detect.return_value = mock_conflict_report

    # Mock do GapDetector
    mock_gap_analysis = MagicMock()
    mock_gap = MagicMock()
    mock_gap.description = "Faltam dados históricos"
    mock_gap.aspect = "historico"
    mock_gap.severity = "high"
    mock_gap_analysis.gaps = [mock_gap]

    orchestrator_mock.gap_detector = AsyncMock()
    orchestrator_mock.gap_detector.detect.return_value = mock_gap_analysis

    # Mock do HITLDialogAgent
    mock_hitl_dialog = AsyncMock()
    # evaluate_finding retorna None para evitar bloquear a execução no teste
    mock_hitl_dialog.evaluate_finding.return_value = None
    orchestrator_mock.hitl_dialog = mock_hitl_dialog

    # Executa o stage
    stage = SynthesizeStage()
    res_context = await stage.run(context)

    # Verifica se evaluate_finding foi chamado para cada tipo de findings gerado
    # Esperamos 4 findings: 1 de contradição, 1 de gap, e 2 de suspicious_source
    calls = mock_hitl_dialog.evaluate_finding.call_args_list
    assert len(calls) == 4

    finding_types = [call[0][1]["type"] for call in calls]
    assert "contradiction" in finding_types
    assert "gap" in finding_types
    assert "suspicious_source" in finding_types


def test_api_dialog_report_endpoint():
    from src.mcp_server import app, get_orchestrator_dep

    orchestrator_mock = MagicMock()
    mock_hitl = MagicMock()
    orchestrator_mock.hitl_dialog = mock_hitl

    # Setup report
    mock_turn = DialogTurn(
        dialog_id="dial_1",
        session_id="session_abc",
        dialog_type=DialogType.PIVOT_DECISION,
        question="Deseja mudar o foco?",
        context="Achado divergente",
        options=["Sim", "Não"],
        urgency_score=0.9,
    )

    # Mock do get_report
    mock_report = MagicMock()
    mock_report.model_dump.return_value = {
        "session_id": "session_abc",
        "dialogs": [mock_turn.__dict__],
        "decisions": [],
    }
    mock_hitl.get_report.return_value = mock_report

    app.dependency_overrides[get_orchestrator_dep] = lambda: orchestrator_mock
    client = TestClient(app)

    try:
        response = client.get("/api/v1/hitl/dialog/report/session_abc")
        assert response.status_code == 200
        assert response.json()["session_id"] == "session_abc"
        assert len(response.json()["dialogs"]) == 1
        mock_hitl.get_report.assert_called_once_with("session_abc")

        # Caso em que o agente não foi inicializado
        orchestrator_mock.hitl_dialog = None
        response = client.get("/api/v1/hitl/dialog/report/session_abc")
        assert response.status_code == 400

    finally:
        app.dependency_overrides.clear()
