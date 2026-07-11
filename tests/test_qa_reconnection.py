"""Testes de reconexão da camada de QA (Fase 6.2).

Valida que MultiLLMFactChecker, RagasEvaluator e TruLensRecorder
são acionados sob demanda no VerificationStage quando
`enable_auditor=True` está no contexto, gravando métricas em
`context.extra['ragas_metrics']`, `['trulens_metrics']` e
`['fact_check_results']`. Com `enable_auditor=False`, nada roda.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.verification_stage import VerificationStage
from src.reasoning.multi_llm_fact_checker import FactCheckResult


def _make_orchestrator(auditor_enabled: bool) -> MagicMock:
    orch = MagicMock()
    mode = MagicMock()
    mode.enable_auditor = auditor_enabled
    orch.operation_mode = mode
    return orch


def _make_llm(verdict: str = "supported") -> MagicMock:
    llm = MagicMock()

    async def _complete(prompt, **kw):
        return (
            f"VERDICT: [{verdict}]\n"
            f"CONFIDENCE: [0.9]\n"
            f"REASONING: ok"
        )

    llm.complete = _complete
    return llm


@pytest.fixture
def ranked_result():
    r = MagicMock()
    r.title = "Claim A"
    r.url = "https://example.com/a"
    r.description = "desc"
    return r


@pytest.mark.asyncio
async def test_qa_runs_when_auditor_enabled(ranked_result):
    orch = _make_orchestrator(auditor_enabled=True)
    ctx = PipelineContext(query="q")
    ctx.extra["orchestrator"] = orch
    ctx.ranked_results = [ranked_result]

    stage = VerificationStage(llm_client=_make_llm())
    await stage.run(ctx)

    assert ctx.extra.get("ragas_metrics") is not None
    assert ctx.extra.get("trulens_metrics") is not None


@pytest.mark.asyncio
async def test_qa_skipped_when_auditor_disabled(ranked_result):
    orch = _make_orchestrator(auditor_enabled=False)
    ctx = PipelineContext(query="q")
    ctx.extra["orchestrator"] = orch
    ctx.ranked_results = [ranked_result]

    stage = VerificationStage(llm_client=_make_llm())
    await stage.run(ctx)

    assert "ragas_metrics" not in ctx.extra
    assert "trulens_metrics" not in ctx.extra


@pytest.mark.asyncio
async def test_fact_checker_records_results(ranked_result):
    """Com claims verificados, fact_check_results é populado (via mock)."""
    orch = _make_orchestrator(auditor_enabled=True)
    ctx = PipelineContext(query="q")
    ctx.extra["orchestrator"] = orch
    ctx.ranked_results = [ranked_result]

    # Faz o loop de verificação produzir um claim verificado sem
    # depender de Docker/LLM: injeta código e um ExecutionResult ok.
    from src.services.code_execution_agent import ExecutionResult

    checker = MagicMock()
    fact_result = FactCheckResult(
        claim="Claim A",
        verdict="supported",
        confidence=0.9,
        consensus=True,
        reasoning="ok",
    )
    checker.verify_batch = AsyncMock(return_value=[fact_result])

    stage = VerificationStage(llm_client=_make_llm(), fact_checker=checker)
    stage._extract_code_with_llm = AsyncMock(return_value="x = 1")
    stage.code_agent.execute_python = lambda code, timeout: ExecutionResult(
        stdout="1", stderr="", exit_code=0, timed_out=False
    )
    await stage.run(ctx)

    results = ctx.extra.get("fact_check_results")
    assert results is not None
    assert len(results) == 1
    assert results[0]["verdict"] == "supported"


@pytest.mark.asyncio
async def test_qa_runs_even_without_ranked_results():
    """QA deve rodar tambem no ramo 'sem resultados rankeados'."""
    orch = _make_orchestrator(auditor_enabled=True)
    ctx = PipelineContext(query="q")
    ctx.extra["orchestrator"] = orch
    ctx.ranked_results = []

    stage = VerificationStage(llm_client=_make_llm())
    await stage.run(ctx)

    assert ctx.extra.get("ragas_metrics") is not None


@pytest.mark.asyncio
async def test_qa_failure_is_non_fatal(ranked_result):
    """Se o Ragas lança, o pipeline nao quebra e trulens ainda roda."""
    orch = _make_orchestrator(auditor_enabled=True)
    ctx = PipelineContext(query="q")
    ctx.extra["orchestrator"] = orch
    ctx.ranked_results = [ranked_result]

    ragas = MagicMock()
    ragas.evaluate = MagicMock(side_effect=RuntimeError("boom"))

    stage = VerificationStage(llm_client=_make_llm(), ragas_evaluator=ragas)
    # Não deve levantar exceção.
    await stage.run(ctx)
    assert ctx.extra.get("trulens_metrics") is not None
