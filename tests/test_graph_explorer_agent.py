"""
test_graph_explorer_agent.py — Testes unitarios para GraphExplorerAgent

Cobre:
  1. kg=None -> retorna relatorio vazio sem erros
  2. Backend incompativel (sem query_graph/get_session_nodes) -> relatorio vazio + warning
  3. Backend com query_graph real -> nos, pontes, gap queries e densidades corretas
  4. GraphGapReport.to_expanded_queries() -> contrato ExpandedQuery
  5. get_community_summary() via detect_communities()
  6. LLM disponivel -> gap query gerada via LLM (com task_type correto)
  7. LLM falha -> fallback sem LLM usado
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.graph_explorer_agent import (
    GraphExplorerAgent,
    GraphGapReport,
    KnowledgeBridge,
    KnowledgeNode,
    MIN_EDGE_COUNT_THRESHOLD,
    MIN_EDGE_WEIGHT_THRESHOLD,
    LLM_TASK_TYPE,
)
from src.knowledge_graph import Triple


# --- Fixtures -----------------------------------------------------------------


def make_triple(subject: str, relation: str, obj: str, confidence: float = 0.9) -> Triple:
    return Triple(subject=subject, relation=relation, object=obj, confidence=confidence, source="test")


def make_kg_with_triples(triples: list) -> MagicMock:
    """Mock de SemanticKnowledgeGraph com query_graph() sincrono."""
    kg = MagicMock()
    del kg.get_session_nodes
    kg.query_graph.return_value = triples
    return kg


# --- Testes -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traverse_no_kg_returns_empty_report():
    """Com kg=None, traverse() devolve relatorio vazio sem lancar excecao."""
    agent = GraphExplorerAgent(knowledge_graph=None)
    report = await agent.traverse(session_id="s1", query_topic="AI")
    assert isinstance(report, GraphGapReport)
    assert report.total_nodes_analyzed == 0
    assert not report.has_gaps
    assert report.gap_severity == "none"
    assert report.gap_queries == []


@pytest.mark.asyncio
async def test_traverse_incompatible_backend_returns_empty_report():
    """Backend sem query_graph nem get_session_nodes -> relatorio vazio."""
    kg = MagicMock(spec=[])
    agent = GraphExplorerAgent(knowledge_graph=kg)
    report = await agent.traverse(session_id="s1", query_topic="AI")
    assert report.total_nodes_analyzed == 0
    assert report.gap_queries == []


@pytest.mark.asyncio
async def test_traverse_with_query_graph_detects_isolated_node():
    """Com query_graph retornando triplas, detecta nos isolados corretamente."""
    triples = [
        make_triple("A", "related", "B"),
        make_triple("A", "related", "C"),
        make_triple("A", "related", "D"),
        make_triple("A", "related", "E"),
        make_triple("A", "related", "F"),
        # No isolado: G aparece apenas 1 vez (< MIN_EDGE_COUNT_THRESHOLD=2)
        make_triple("G", "mentions", "Lone"),
    ]
    kg = make_kg_with_triples(triples)
    agent = GraphExplorerAgent(knowledge_graph=kg, llm=None)
    report = await agent.traverse(session_id="s1", query_topic="test")

    assert report.total_nodes_analyzed > 0
    isolated_ids = {n.node_id for n in report.isolated_nodes}
    assert "G" in isolated_ids or "Lone" in isolated_ids
    assert "A" not in isolated_ids


@pytest.mark.asyncio
async def test_traverse_density_fully_connected():
    """Densidade == 1.0 quando todos os nos tem edge_count >= MIN_EDGE_COUNT_THRESHOLD."""
    triples = [
        make_triple("A", "r", "B"),
        make_triple("A", "r", "C"),
        make_triple("B", "r", "C"),
    ]
    kg = make_kg_with_triples(triples)
    agent = GraphExplorerAgent(knowledge_graph=kg)
    report = await agent.traverse(session_id="s1")

    assert 0.0 <= report.graph_density <= 1.0
    assert report.graph_density == 1.0


@pytest.mark.asyncio
async def test_gap_queries_generated_without_llm():
    """Sem LLM, gap queries usam fallback de template baseado no label do no."""
    triples = [make_triple("Orphan", "mentions", "Lone")]
    kg = make_kg_with_triples(triples)
    agent = GraphExplorerAgent(knowledge_graph=kg, llm=None)
    report = await agent.traverse(session_id="s1", query_topic="machine learning")

    assert len(report.gap_queries) > 0
    for q in report.gap_queries:
        assert isinstance(q, str)
        assert len(q) > 0


@pytest.mark.asyncio
async def test_gap_queries_fallback_when_llm_raises():
    """Quando LLM lanca excecao, usa fallback sem propagar o erro."""
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    triples = [make_triple("IsolatedNode", "related", "SoloNode")]
    kg = make_kg_with_triples(triples)
    agent = GraphExplorerAgent(knowledge_graph=kg, llm=mock_llm)

    report = await agent.traverse(session_id="s1", query_topic="topic")
    assert len(report.gap_queries) > 0


@pytest.mark.asyncio
async def test_gap_queries_use_llm_when_available():
    """Com LLM disponivel, gap query e gerada via LLM com task_type correto."""
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="best sources for isolated concept")

    triples = [make_triple("Orphan", "related", "Solo")]
    kg = make_kg_with_triples(triples)
    agent = GraphExplorerAgent(knowledge_graph=kg, llm=mock_llm)
    report = await agent.traverse(session_id="s1", query_topic="topic")

    if report.isolated_nodes:
        mock_llm.complete.assert_called()
        call = mock_llm.complete.call_args
        # Verifica task_type passado como kwarg
        assert call.kwargs.get("task_type") == LLM_TASK_TYPE


def test_graph_gap_report_to_expanded_queries():
    """to_expanded_queries() retorna lista de ExpandedQuery com contrato correto."""
    from src.types import ExpandedQuery

    report = GraphGapReport(
        session_id="s1",
        gap_queries=["query one", "query two"],
    )
    report.isolated_nodes = [KnowledgeNode(node_id="n1", label="n1")]

    expanded = report.to_expanded_queries(priority="alta")

    assert len(expanded) == 2
    for eq in expanded:
        assert isinstance(eq, ExpandedQuery)
        assert eq.type == "graph_gap"
        assert eq.priority == "alta"
        assert "s1" in eq.rationale
        assert eq.query in ("query one", "query two")


def test_graph_gap_report_to_expanded_queries_empty():
    """to_expanded_queries() com gap_queries vazio retorna lista vazia."""
    report = GraphGapReport(session_id="s2", gap_queries=[])
    assert report.to_expanded_queries() == []


@pytest.mark.asyncio
async def test_get_community_summary_via_detect_communities():
    """get_community_summary() usa detect_communities() quando disponivel."""
    kg = MagicMock()
    del kg.get_communities
    kg.detect_communities.return_value = [
        {"A", "B", "C"},
        {"X", "Y"},
    ]
    agent = GraphExplorerAgent(knowledge_graph=kg)
    summary = await agent.get_community_summary()

    kg.detect_communities.assert_called_once()
    assert len(summary) == 2
    assert summary[0]["size"] == 3
    assert summary[1]["size"] == 2


@pytest.mark.asyncio
async def test_get_community_summary_no_kg():
    """Com kg=None, get_community_summary() retorna dict vazio."""
    agent = GraphExplorerAgent(knowledge_graph=None)
    summary = await agent.get_community_summary()
    assert summary == {}


def test_knowledge_node_defaults():
    """KnowledgeNode tem valores padrao sensiveis apos a correcao."""
    n = KnowledgeNode(node_id="x", label="X")
    assert n.node_type == "entity"
    assert n.edge_count == 0
    assert n.community_id == -1
    assert n.embedding_available is False


def test_gap_report_severity_levels():
    """gap_severity escala corretamente com o numero de gaps."""
    report = GraphGapReport(session_id="s1")
    assert report.gap_severity == "none"

    report.isolated_nodes = [KnowledgeNode(node_id=str(i), label=str(i)) for i in range(2)]
    assert report.gap_severity == "low"

    report.isolated_nodes = [KnowledgeNode(node_id=str(i), label=str(i)) for i in range(7)]
    assert report.gap_severity == "medium"

    report.isolated_nodes = [KnowledgeNode(node_id=str(i), label=str(i)) for i in range(10)]
    assert report.gap_severity == "high"
