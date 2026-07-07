"""tests/test_graph_explorer_stage_smoke.py — Smoke tests para o GraphExplorerStage.

Valida a lógica nova do stage isoladamente cobrindo os cenários:
  1. Caminho feliz (gaps encontrados -> novas queries injetadas)
  2. Deduplicação (queries de gap não duplicam se já existentes)
  3. Relatório vazio (has_gaps=False)
  4. Falha tratada (traverse levanta exceção, stage não quebra porque critical=False)
  5. Resolução de agente com/sem memória via extras
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graph_explorer_agent import GraphExplorerAgent, GraphGapReport
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.graph_explorer_stage import GraphExplorerStage
from src.types import ExpandedQuery


@pytest.fixture
def empty_context() -> PipelineContext:
    ctx = PipelineContext(query="qualquer topico")
    # Simula o extras com orquestrador mockado
    ctx.extras["session_id"] = "session_test"
    orchestrator = MagicMock()
    orchestrator.llm = MagicMock()
    orchestrator.memory = MagicMock()
    # Mock do kg (SemanticKnowledgeGraph)
    orchestrator.memory.kg = MagicMock()
    ctx.extras["orchestrator"] = orchestrator
    return ctx


@pytest.mark.asyncio
async def test_graph_explorer_stage_happy_path(empty_context):
    # Setup report com gaps
    mock_report = GraphGapReport(
        session_id="session_test",
        total_nodes_analyzed=10,
        isolated_nodes=[MagicMock()],
        weak_bridges=[MagicMock()],
        gap_queries=["gap query 1", "gap query 2"],
        community_count=2,
        graph_density=0.1,
    )
    
    mock_agent = AsyncMock(spec=GraphExplorerAgent)
    mock_agent.traverse.return_value = mock_report

    stage = GraphExplorerStage(graph_explorer_agent=mock_agent)
    
    res_context = await stage.run(empty_context)
    
    assert res_context.gap_analysis is mock_report
    assert len(res_context.expanded_queries) == 2
    assert res_context.expanded_queries[0].query == "gap query 1"
    assert res_context.expanded_queries[1].query == "gap query 2"
    mock_agent.traverse.assert_called_once_with(
        session_id="session_test", query_topic="qualquer topico"
    )


@pytest.mark.asyncio
async def test_graph_explorer_stage_deduplication(empty_context):
    # Queries já existentes no contexto
    empty_context.expanded_queries.append(
        ExpandedQuery(query="gap query 1", type="original", priority="alta")
    )
    
    mock_report = GraphGapReport(
        session_id="session_test",
        total_nodes_analyzed=5,
        isolated_nodes=[MagicMock()],
        gap_queries=["gap query 1", "new gap query"],
    )
    mock_agent = AsyncMock(spec=GraphExplorerAgent)
    mock_agent.traverse.return_value = mock_report

    stage = GraphExplorerStage(graph_explorer_agent=mock_agent)
    res_context = await stage.run(empty_context)
    
    # Apenas a não existente deve ser adicionada
    assert len(res_context.expanded_queries) == 2
    assert res_context.expanded_queries[1].query == "new gap query"


@pytest.mark.asyncio
async def test_graph_explorer_stage_empty_report_no_gaps(empty_context):
    # Sem gaps
    mock_report = GraphGapReport(
        session_id="session_test",
        total_nodes_analyzed=15,
        isolated_nodes=[],
        weak_bridges=[],
        gap_queries=[],
    )
    mock_agent = AsyncMock(spec=GraphExplorerAgent)
    mock_agent.traverse.return_value = mock_report

    stage = GraphExplorerStage(graph_explorer_agent=mock_agent)
    res_context = await stage.run(empty_context)
    
    assert res_context.gap_analysis is mock_report
    assert len(res_context.expanded_queries) == 0


@pytest.mark.asyncio
async def test_graph_explorer_stage_failure_is_non_critical(empty_context):
    # Simula que o traverse lança exceção
    mock_agent = AsyncMock(spec=GraphExplorerAgent)
    mock_agent.traverse.side_effect = Exception("KuzuDB Connection Error")

    stage = GraphExplorerStage(graph_explorer_agent=mock_agent)
    
    # Não deve levantar exceção pois critical = False
    res_context = await stage.run(empty_context)
    
    assert res_context.gap_analysis is None
    assert len(res_context.expanded_queries) == 0


@pytest.mark.asyncio
async def test_graph_explorer_stage_resolves_agent_from_context_successfully(empty_context):
    stage = GraphExplorerStage()
    # Testa resolução dinâmica do agente usando extras do pipeline
    agent = stage._resolve_agent(empty_context)
    assert isinstance(agent, GraphExplorerAgent)
    assert agent._kg is empty_context.extras["orchestrator"].memory.kg
    assert agent._llm is empty_context.extras["orchestrator"].llm


@pytest.mark.asyncio
async def test_graph_explorer_stage_resolves_agent_without_memory(empty_context):
    # Remove a memória para simular ausência
    empty_context.extras["orchestrator"].memory = None
    stage = GraphExplorerStage()
    agent = stage._resolve_agent(empty_context)
    assert isinstance(agent, GraphExplorerAgent)
    assert agent._kg is None
