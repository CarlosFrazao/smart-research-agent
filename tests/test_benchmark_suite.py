"""
Testes do Benchmark Suite (Bloco 15 / E1-T3) — SRA vs. Perplexity/Gemini.

Todos os testes rodam em modo dry-run (fixtures, zero rede). Validam:
- construção determinística dos 3 backends a partir de fixtures;
- o Evaluator produz faithfulness/traceability corretos via QualityGate (Bloco 6);
- o ranking SRA > Perplexity > Gemini em recall de claims (Bloco 5);
- o relatório Markdown é gerado sem erro e contém as seções esperadas;
- nenhuma chamada de rede ocorre em dry-run (offline).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.benchmark.benchmark_suite import (
    ANCHOR_QUERIES,
    BackendAnswer,
    BenchmarkRunner,
    Evaluator,
    GeminiBackend,
    PerplexityBackend,
    SraBackend,
    _build_claim_objects,
)

FIXTURES = Path(__file__).parent / "benchmark" / "fixtures"


@pytest.fixture
def runner_dry() -> BenchmarkRunner:
    return BenchmarkRunner(
        backends=[
            SraBackend(dry_run=True),
            PerplexityBackend(dry_run=True),
            GeminiBackend(dry_run=True),
        ],
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_anchor_queries_have_five_domains():
    """20 queries distribuídas em exatamente 5 domínios."""
    assert len(ANCHOR_QUERIES) == 20
    domains = {q["domain"] for q in ANCHOR_QUERIES}
    assert domains == {"tech", "biomedical", "economics", "legal", "general"}
    per_domain = {}
    for q in ANCHOR_QUERIES:
        per_domain[q["domain"]] = per_domain.get(q["domain"], 0) + 1
    assert all(count == 4 for count in per_domain.values())


@pytest.mark.asyncio
async def test_sra_backend_dry_run_reads_fixture():
    backend = SraBackend(dry_run=True)
    q = ANCHOR_QUERIES[0]["query"]
    answer = await backend.answer(q)
    assert answer.success
    assert answer.backend == "sra"
    assert answer.claims  # fixture tem claims
    assert all(c.get("source_ids") or c.get("urls") for c in answer.claims[:5])


@pytest.mark.asyncio
async def test_competitor_backends_dry_run_read_fixtures():
    for backend_cls in (PerplexityBackend, GeminiBackend):
        backend = backend_cls(dry_run=True)
        q = ANCHOR_QUERIES[1]["query"]
        answer = await backend.answer(q)
        assert answer.success, f"{backend.name} deveria ler fixture"
        assert answer.backend == backend.name
        assert answer.report


@pytest.mark.asyncio
async def test_evaluator_quality_gate_scores():
    """QualityGate (Bloco 6) produz faithfulness/traceability > 0 para claims fundamentadas."""
    answer = BackendAnswer(
        backend="sra",
        query="q",
        report="rel",
        claims=[
            {
                "text": "a",
                "source_ids": ["s1"],
                "urls": ["https://x/1"],
                "confidence": 0.9,
            },
            {
                "text": "b",
                "source_ids": ["s2"],
                "urls": ["https://x/2"],
                "confidence": 0.8,
            },
        ],
        contexts=["ctx1", "ctx2"],
    )
    ev = await Evaluator().evaluate(answer, "q", "tech")
    assert ev.success
    assert ev.faithfulness > 0.0
    assert ev.traceability > 0.0
    assert ev.recall == 1.0


@pytest.mark.asyncio
async def test_evaluator_ungrounded_claims_low_recall():
    answer = BackendAnswer(
        backend="x",
        query="q",
        report="rel",
        claims=[{"text": "a", "source_ids": [], "urls": [], "confidence": 0.5}],
        contexts=[],
    )
    ev = await Evaluator().evaluate(answer, "q", "general")
    assert ev.recall == 0.0
    assert ev.faithfulness == 0.0
    assert ev.traceability == 0.0


@pytest.mark.asyncio
async def test_build_claim_objects_returns_synthesized_claim():
    from src.types import SynthesizedClaim

    objs = _build_claim_objects(
        [{"text": "t", "source_ids": ["s"], "urls": ["u"], "confidence": 0.9}]
    )
    assert objs and isinstance(objs[0], SynthesizedClaim)
    assert objs[0].source_ids == ["s"]


@pytest.mark.asyncio
async def test_ranking_sra_beats_competitors_on_recall(runner_dry):
    """SRA (fixture 0.85) > Perplexity (0.70) > Gemini (0.55) em recall."""
    evals = await runner_dry.run()
    by_backend = {}
    for ev in evals:
        by_backend.setdefault(ev.backend, []).append(ev.recall)
    import statistics

    sra = statistics.mean(by_backend["sra"])
    perp = statistics.mean(by_backend["perplexity"])
    gem = statistics.mean(by_backend["gemini"])
    assert sra > perp > gem
    # SRA recall ~0.85 conforme fixture
    assert sra >= 0.8


@pytest.mark.asyncio
async def test_runner_writes_report_no_network(runner_dry, tmp_path, monkeypatch):
    """Dry-run gera relatório com seções esperadas e sem chamadas de rede."""
    runner_dry.out_dir = tmp_path / "benchmark_results"
    # Garante que nenhum backend tente rede: monkeypatch httpx para falhar se chamado
    import httpx

    def _no_http(*a, **k):
        raise AssertionError("rede não deveria ser chamada em dry-run")

    monkeypatch.setattr(httpx, "AsyncClient", _no_http)

    evals = await runner_dry.run()
    assert len(evals) == 60  # 20 queries x 3 backends
    path = runner_dry.write_report(evals)
    text = path.read_text(encoding="utf-8")
    assert "Benchmark SRA vs. Perplexity/Gemini" in text
    assert "Resumo por Backend" in text
    assert "Detalhe por Query" in text
    assert "dry-run" in text


@pytest.mark.asyncio
async def test_cli_dry_run_entrypoint():
    from tests.benchmark import benchmark_suite

    # Exercita o entrypoint assíncrono real (equivalente a `main --dry-run`,
    # mas sem asyncio.run pois já estamos num loop ativo do pytest-asyncio).
    code = await benchmark_suite._amain(dry_run=True)
    assert code == 0
    # O relatório de produção é escrito em benchmark_results/ (limpo em seguida).
    from tests.benchmark.benchmark_suite import BenchmarkRunner

    out = BenchmarkRunner(backends=[], dry_run=True).out_dir
    reports = list(out.glob("report_*.md"))
    assert reports
    for r in reports:
        r.unlink()
