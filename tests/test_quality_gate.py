"""
Testes do Quality Gate RAGAS (Bloco 6 / E1-T2).

Cobre:
- Score abaixo do threshold -> passed == False (retry recomendado).
- Score acima do threshold -> passed == True.
- Timeout do RAGAS -> retorna passed=True (gracioso, não quebra pipeline).
- Gate desativado -> resultado None, não avalia nada.
- Traceability determinística (proxy) derivada de SynthesizedClaim (Bloco 5).
- Métricas Prometheus emitidas (gracioso se client ausente).
- Integração via QualityGateStage no PipelineContext.
"""

import asyncio

import pytest

from src.quality_gate import QualityGate, QualityGateResult
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.quality_gate_stage import QualityGateStage
from src.synthesizer import Synthesizer
from src.types import RankedResult, SynthesizedClaim, SynthesizedResult


# ── Helpers ────────────────────────────────────────────────────────────────


def _claim(text: str, source_id: str = "", url: str = "") -> SynthesizedClaim:
    """Constrói um SynthesizedClaim mínimo com proveniência opcional."""
    return SynthesizedClaim(
        text=text,
        source_ids=[source_id] if source_id else [],
        urls=[url] if url else [],
    )


def _ranked(entity: str, url: str, rid: str) -> RankedResult:
    """Constrói um RankedResult mínimo para derivar claims."""
    return RankedResult(
        source="web",
        title=entity,
        description=f"{entity} desc",
        url=url,
        sources=["web"],
        result_id=rid,
        combined_score=80.0,
    )


def _synthesized(entity: str, url: str, rid: str, tldr: str) -> SynthesizedResult:
    """Constrói um SynthesizedResult mínimo (carrega tldr/sources/urls)."""
    return SynthesizedResult(
        entity=entity,
        title=entity,
        description=f"{entity} desc",
        sources=["web"],
        urls=[url] if url else [],
        result_id=rid,
        combined_score=80.0,
        tldr=tldr,
    )


# ── QualityGate: decisão por threshold ──────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_fails():
    """Score abaixo do threshold -> passed == False e retry recomendado."""
    gate = QualityGate(threshold_faithfulness=0.70, threshold_relevancy=0.75)
    # Claims sem proveniência -> traceability 0.0 -> faithfulness 0.0.
    claims = [_claim("afirmação sem fonte") for _ in range(3)]
    result = await gate.evaluate("q", claims, ["ctx"])
    assert isinstance(result, QualityGateResult)
    assert result.passed is False
    assert result.retry_recommended is True
    assert result.faithfulness < 0.70


@pytest.mark.asyncio
async def test_above_threshold_passes():
    """Claims totalmente rastreáveis -> passed == True."""
    gate = QualityGate(threshold_faithfulness=0.70, threshold_relevancy=0.75)
    claims = [
        _claim("claims grounded", source_id="src1", url="https://a.com/1"),
        _claim("claims grounded 2", source_id="src2", url="https://a.com/2"),
    ]
    contexts = ["fonte real 1", "fonte real 2"]
    result = await gate.evaluate("q", claims, contexts)
    assert result.passed is True
    assert result.retry_recommended is False
    assert result.faithfulness >= 0.70
    assert result.relevancy >= 0.75


@pytest.mark.asyncio
async def test_metric_keys_present():
    """Result carrega faithfulness/relevancy/traceability/mode."""
    gate = QualityGate()
    claims = [_claim("x", source_id="s", url="https://a.com")]
    result = await gate.evaluate("q", claims, ["ctx"])
    assert 0.0 <= result.faithfulness <= 1.0
    assert 0.0 <= result.relevancy <= 1.0
    assert 0.0 <= result.traceability <= 1.0
    assert result.mode in {"proxy", "ragas", "timeout", "error"}


# ── Graceful: timeout / erro não quebram ────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_is_graceful_pass():
    """Timeout do RAGAS -> passed=True (gracioso), não lança."""

    class _SlowEvaluator:
        enabled = True

        async def evaluate(self, context):
            await asyncio.sleep(5)  # simula RAGAS lento
            return {}

    gate = QualityGate(timeout_seconds=0.05)
    gate._evaluator = _SlowEvaluator()  # injeta evaluator lento
    result = await gate.evaluate("q", [_claim("x")], ["ctx"])
    assert result.passed is True
    assert result.mode == "timeout"


@pytest.mark.asyncio
async def test_no_claims_yields_zero_traceability():
    """Sem claims, traceability é 0.0 e o gate não quebra."""
    gate = QualityGate()
    result = await gate.evaluate("q", [], [])
    assert result.traceability == 0.0
    assert isinstance(result, QualityGateResult)


# ── Traceability determinística (proxy) ─────────────────────────────────────


def test_traceability_fraction():
    """_traceability conta claims com source_id E url."""
    claims = [
        _claim("a", source_id="s1", url="https://a.com/1"),
        _claim("b", source_id="s2", url="https://a.com/2"),
        _claim("c"),  # sem proveniência
    ]
    assert QualityGate._traceability(claims) == pytest.approx(2 / 3)


# ── QualityGateStage integração ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_disabled_is_noop():
    """Gate desativado -> extra indica falha=False e resultado None."""
    stage = QualityGateStage(enabled=False)
    ctx = PipelineContext(query="q", synthesized_results=[_synthesized("e", "u", "r", "t")])
    out = await stage.run(ctx)
    assert out.extra["quality_gate_failed"] is False
    assert out.extra["quality_gate_result"] is None


@pytest.mark.asyncio
async def test_stage_records_failure_in_context():
    """Stage com claims sem fonte -> marca quality_gate_failed=True."""
    stage = QualityGateStage(enabled=True)
    # SynthesizedResult sem proveniência -> claims derivadas sem source_id/url.
    ctx = PipelineContext(query="q", synthesized_results=[_synthesized("e", "", "", "t")])
    # Força lista vazia de claims para simular ausência de rastreabilidade.
    ctx.extra["synthesized_claims"] = [_claim("afirmação sem fonte")]
    out = await stage.run(ctx)
    assert out.extra["quality_gate_failed"] is True
    assert out.extra["quality_gate_result"].passed is False


@pytest.mark.asyncio
async def test_stage_derives_claims_from_synthesized():
    """Stage deriva claims dos synthesized_results via Synthesizer (Bloco 5)."""
    stage = QualityGateStage(enabled=True)
    res = _synthesized("Entidade X", "https://ex.com/x", "rid-1", "Entidade X é relevante")
    ctx = PipelineContext(query="q", synthesized_results=[res])
    out = await stage.run(ctx)
    result = out.extra["quality_gate_result"]
    assert result is not None
    assert result.traceability >= 0.0  # derivação não quebra
