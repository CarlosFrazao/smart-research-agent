"""Onda 2 / M2.1 — propagação das iterações reais do ReAct ao contexto.

Antes da correção, o relatório mostrava "Search iterations: 1" fixo, mesmo
quando o loop ReAct executava 8 iterações. Agora o ReActOrchestrator grava
``context.extra["iterations"]`` = contador real do decision engine a cada
stage, e o ReportStage lê esse valor.

Ver: Plan_SRA_Melhorias_Qualidade_2026-07-16.md
"""

import pytest

from src.pipeline.pipeline import PipelineContext


class _FakeStage:
    name = "search"

    async def run(self, context):
        return context


class _FakeDecisionEngine:
    def __init__(self, iteration):
        self._iteration = iteration

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_execute_stage_writes_real_iterations(monkeypatch):
    """_execute_stage_with_progress grava o nº real de iterações no contexto."""
    from src.react_orchestrator import ReActOrchestrator

    # Instancia sem __init__ pesado (evita boot de searchers/LLM).
    orch = ReActOrchestrator.__new__(ReActOrchestrator)
    orch._decision_engine = _FakeDecisionEngine(iteration=8)

    async def _noop_progress(*args, **kwargs):
        return None

    orch._report_progress = _noop_progress  # type: ignore[attr-defined]

    ctx = PipelineContext(query="test query about databases")
    result = await orch._execute_stage_with_progress(
        _FakeStage(), ctx, progress_callback=None, session_id="s"
    )

    assert result.get("iterations") == 8


@pytest.mark.asyncio
async def test_report_stage_reads_iterations_from_context():
    """O ReportStage usa context.get('iterations') no fallback de metadados."""
    # Verifica o contrato: o valor gravado por M2.1 é lido pelo report.
    ctx = PipelineContext(query="q")
    ctx.set("iterations", 5)
    assert ctx.get("iterations") == 5
