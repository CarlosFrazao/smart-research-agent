"""
test_hitl_decision.py — Testes para a aplicação de decisões HITL no Orchestrator
(Tarefa 2.2 da Fase 2 da Auditoria Parte 2).

Cobrem o ramo `veto`/`exclude_source` (filtragem de resultados + registro em
feedback_store) e o ramo `expand_scope`/`expand` (registro do hint no contexto).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import asyncio

import pytest

from src.orchestrator import Orchestrator
from src.pipeline.pipeline import PipelineContext


class _FakeResult:
    """Resultado mínimo com atributo ``source`` para simular ranked_results."""

    def __init__(self, source: str) -> None:
        self.source = source


@pytest.fixture
def orchestrator() -> Orchestrator:
    """Retorna um Orchestrator sem feedback_store (estado atual do código)."""
    orch = Orchestrator.__new__(Orchestrator)
    return orch


def _make_context(results: list[Any]) -> PipelineContext:
    ctx = PipelineContext(query="test query")
    ctx.ranked_results = list(results)
    return ctx


def test_veto_filters_results_from_source(orchestrator: Orchestrator) -> None:
    """O ramo veto remove da ranked_results os resultados da fonte vetada."""
    ctx = _make_context(
        [
            _FakeResult("github"),
            _FakeResult("reddit"),
            _FakeResult("github"),
            _FakeResult("hackernews"),
        ]
    )
    decision = {"action": "veto", "parameters": {"source": "github"}}

    asyncio.run(orchestrator._apply_hitl_decision(decision, ctx))

    remaining_sources = [r.source for r in ctx.ranked_results]
    assert remaining_sources == ["reddit", "hackernews"]
    assert len(ctx.ranked_results) == 2


def test_veto_records_negative_signal_when_feedback_store_present(
    orchestrator: Orchestrator,
) -> None:
    """Quando feedback_store existe, o veto registra sinal negativo."""
    recorded: list[dict[str, Any]] = []

    class _FakeFeedbackStore:
        def record(self, **kwargs: Any) -> dict[str, Any]:
            recorded.append(kwargs)
            return kwargs

    orchestrator.feedback_store = _FakeFeedbackStore()
    ctx = _make_context([_FakeResult("github"), _FakeResult("reddit")])
    ctx.user_id = "user-42"
    decision = {"action": "exclude_source", "parameters": {"source": "github"}}

    asyncio.run(orchestrator._apply_hitl_decision(decision, ctx))

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["signal"] == "not_useful"
    assert entry["source_name"] == "github"
    assert entry["result_id"] == "hitl_veto:github"
    assert entry["user_id"] == "user-42"


def test_veto_without_source_is_ignored(orchestrator: Orchestrator) -> None:
    """Veto sem fonte definida não quebra e não altera ranked_results."""
    ctx = _make_context([_FakeResult("github"), _FakeResult("reddit")])
    decision = {"action": "veto", "parameters": {}}

    asyncio.run(orchestrator._apply_hitl_decision(decision, ctx))

    assert len(ctx.ranked_results) == 2


def test_expand_scope_registers_hint(orchestrator: Orchestrator) -> None:
    """O ramo expand_scope registra o hint em context.expand_hints."""
    ctx = _make_context([_FakeResult("github")])
    decision = {"action": "expand_scope", "parameters": {"hint": "buscar fontes acadêmicas"}}

    asyncio.run(orchestrator._apply_hitl_decision(decision, ctx))

    assert hasattr(ctx, "expand_hints")
    assert "buscar fontes acadêmicas" in ctx.expand_hints
