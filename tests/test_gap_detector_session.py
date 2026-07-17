"""Testes de FEAT-002: GapDetector consome o índice de sessões passadas.

Cobrem o enriquecimento de contexto de gap-detection via SessionSearchIndex
(FEAT-001), garantindo:
  - índice populado -> hits anexados e consultados;
  - índice vazio -> sem quebra (lista vazia);
  - session_index=None -> comportamento anterior (sem enriquecimento);
  - erro no índice -> degradação graciosa (lista vazia, sem exceção).
"""

from __future__ import annotations

import types

import pytest

from src.gap_detector import GapDetector
from src.memory.session_index import SessionSearchIndex
from src.types import GapAnalysis, IntentResult, RankedResult


def _make_fake_llm(raise_on_call: bool = False) -> object:
    """LLMClient mínimo: retorna GapAnalysis ou levanta (para testar fallback)."""
    llm = types.SimpleNamespace()
    if raise_on_call:

        async def generate_structured(*_args, **_kwargs):
            raise RuntimeError("LLM indisponível (simulado)")

        llm.generate_structured = generate_structured
    else:

        async def generate_structured(*_args, **_kwargs):
            return GapAnalysis(
                is_complete=True,
                missing_aspects=[],
                new_queries=[],
                confidence="alta",
                rationale="analise via LLM",
            ).model_dump()

        llm.generate_structured = generate_structured
    llm.token_economy = None
    return llm


def _make_result(title: str, source: str = "web") -> RankedResult:
    return RankedResult(
        source=source,
        title=title,
        url=f"https://example.com/{title}",
        description="desc",
        metrics={},
        raw={},
        fetched_at="2026-01-01T00:00:00Z",
        published_at="2026-01-01T00:00:00Z",
        confidence_score=0.9,
        evidence_quality="verified",
        citations=[],
        contradictions=[],
        hallucination_flags=[],
        result_id=f"r-{title}",
        cluster_id="0",
        corroborated_by=[],
        lineage_role="primary",
        cites_within_cluster=[],
        is_adversarial=False,
        trust_tier="allow",
        score=0.9,
        score_breakdown={},
    )


def _make_intent() -> IntentResult:
    from src.types import Domain, Intention

    return IntentResult(
        domain=Domain.OPEN_SOURCE,
        entities=[],
        intention=Intention.LEARN,
        urgency="nao",
        confidence="alta",
    )


def _populated_index() -> SessionSearchIndex:
    idx = SessionSearchIndex(":memory:")
    idx.index("concorrencia em python asyncio", "reports/prev1.md")
    idx.index("deadlock mutex c++", "reports/prev2.md")
    idx.index("starvation scheduler linux", "reports/prev3.md")
    return idx


# ── Testes de recuperação de contexto (unidade do método) ──────────────────


def test_retrieve_returns_hits_when_index_populated():
    detector = GapDetector(_make_fake_llm(), session_index=_populated_index())
    hits = detector._retrieve_session_context("concorrencia python")
    assert len(hits) >= 1
    assert any("concorrencia" in (h["query"] or "").lower() for h in hits)


def test_retrieve_empty_when_index_empty():
    idx = SessionSearchIndex(":memory:")  # sem index()
    detector = GapDetector(_make_fake_llm(), session_index=idx)
    assert detector._retrieve_session_context("qualquer coisa") == []


def test_retrieve_empty_when_no_index():
    detector = GapDetector(_make_fake_llm())  # session_index=None
    assert detector._retrieve_session_context("qualquer coisa") == []


def test_retrieve_graceful_on_index_error(monkeypatch):
    idx = _populated_index()
    # Força erro na busca para simular índice corrompido/indisponível.
    monkeypatch.setattr(idx, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB off")))
    detector = GapDetector(_make_fake_llm(), session_index=idx)
    # Não deve levantar — degradação graciosa.
    assert detector._retrieve_session_context("concorrencia") == []


# ── Teste de integração: detect() enriquece sem quebrar ─────────────────────


@pytest.mark.asyncio
async def test_detect_with_session_index_no_break():
    detector = GapDetector(_make_fake_llm(), session_index=_populated_index())
    results = [
        _make_result(f"org/proj{i}", source=["web", "github", "reddit", "arxiv", "hn"][i % 5])
        for i in range(12)
    ]
    analysis = await detector.detect(
        results, "concorrencia em python", _make_intent()
    )
    # Com índice presente e fontes diversas, o detect() deve completar sem
    # exceção (enriquecimento de contexto não quebra o fluxo).
    assert isinstance(analysis, GapAnalysis)
    assert analysis.is_complete is True


@pytest.mark.asyncio
async def test_detect_without_session_index_unchanged():
    detector = GapDetector(_make_fake_llm())  # sem índice
    results = [
        _make_result(f"org/proj{i}", source=["web", "github", "reddit", "arxiv", "hn"][i % 5])
        for i in range(12)
    ]
    analysis = await detector.detect(
        results, "concorrencia em python", _make_intent()
    )
    # Sem índice, o comportamento é idêntico (sem enriquecimento) e conclui OK.
    assert isinstance(analysis, GapAnalysis)
    assert analysis.is_complete is True
