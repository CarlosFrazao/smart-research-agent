"""Testes do veredito rico P1: Verdict enum, _compute_verdict, SynthesizedResult."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.types import SynthesizedResult, Verdict
from src.synthesizer import Synthesizer
from src.types import RankedResult


# ── Verdict enum ──────────────────────────────────────────────────────────────

class TestVerdictEnum:
    def test_foca_value(self):
        assert Verdict.FOCA.value == "Foca"

    def test_considera_value(self):
        assert Verdict.CONSIDERA.value == "Considera"

    def test_acompanha_value(self):
        assert Verdict.ACOMPANHA.value == "Acompanha"

    def test_ignora_value(self):
        assert Verdict.IGNORA.value == "Ignora"

    def test_all_four_verdicts_exist(self):
        values = {v.value for v in Verdict}
        assert values == {"Foca", "Considera", "Acompanha", "Ignora"}


# ── _compute_verdict ─────────────────────────────────────────────────────────

class TestComputeVerdict:
    def _compute(self, score: float, description: str = "desc", highlights: list = None):
        return Synthesizer._compute_verdict(score, description, highlights or [])

    def test_score_80_is_foca(self):
        verdict, _, _, _ = self._compute(80.0)
        assert verdict == "Foca"

    def test_score_75_is_foca(self):
        verdict, _, _, _ = self._compute(75.0)
        assert verdict == "Foca"

    def test_score_74_is_considera(self):
        verdict, _, _, _ = self._compute(74.9)
        assert verdict == "Considera"

    def test_score_50_is_considera(self):
        verdict, _, _, _ = self._compute(50.0)
        assert verdict == "Considera"

    def test_score_49_is_acompanha(self):
        verdict, _, _, _ = self._compute(49.9)
        assert verdict == "Acompanha"

    def test_score_30_is_acompanha(self):
        verdict, _, _, _ = self._compute(30.0)
        assert verdict == "Acompanha"

    def test_score_29_is_ignora(self):
        verdict, _, _, _ = self._compute(29.9)
        assert verdict == "Ignora"

    def test_score_0_is_ignora(self):
        verdict, _, _, _ = self._compute(0.0)
        assert verdict == "Ignora"

    def test_tldr_truncates_long_description(self):
        long_desc = "x" * 200
        _, tldr, _, _ = self._compute(80.0, long_desc)
        assert len(tldr) <= 130  # 120 chars + "…" + highlight bracket

    def test_tldr_includes_highlight(self):
        _, tldr, _, _ = self._compute(80.0, "desc curta", ["5000 stars no GitHub"])
        assert "5000 stars no GitHub" in tldr

    def test_tldr_without_highlights(self):
        _, tldr, _, _ = self._compute(80.0, "descricao limpa", [])
        assert "descricao limpa" in tldr
        assert "[" not in tldr

    def test_next_step_foca(self):
        _, _, next_step, _ = self._compute(80.0)
        assert "Avaliar" in next_step or "testar" in next_step

    def test_next_step_considera(self):
        _, _, next_step, _ = self._compute(60.0)
        assert "Agendar" in next_step or "relevância contextual" in next_step

    def test_next_step_acompanha(self):
        _, _, next_step, _ = self._compute(40.0)
        assert "revisão" in next_step or "tangencial" in next_step

    def test_next_step_ignora(self):
        _, _, next_step, _ = self._compute(10.0)
        assert "Dispensar" in next_step or "fora do escopo" in next_step

    def test_read_min_min_2(self):
        _, _, _, read_min = self._compute(80.0, "curto", [])
        assert read_min >= 2

    def test_read_min_max_10(self):
        long_desc = "palavra " * 2000
        _, _, _, read_min = self._compute(80.0, long_desc, ["h1", "h2", "h3"])
        assert read_min <= 10

    def test_read_min_scales_with_content(self):
        _, _, _, rm_short = self._compute(80.0, "abc", [])
        _, _, _, rm_long = self._compute(80.0, "palavra " * 500, ["destaque " * 50])
        assert rm_long >= rm_short


# ── SynthesizedResult com campos de veredito ─────────────────────────────────

class TestSynthesizedResultVerdictFields:
    def _make_result(self, **kwargs) -> SynthesizedResult:
        defaults = dict(
            entity="test",
            title="Test Project",
            description="A test project description",
            sources=["github"],
            urls=["https://github.com/test/test"],
            combined_score=80.0,
            metrics={},
            highlights=["1000 stars no GitHub"],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
        )
        defaults.update(kwargs)
        return SynthesizedResult(**defaults)

    def test_verdict_field_defaults_empty(self):
        r = self._make_result()
        assert r.verdict == ""

    def test_verdict_field_accepts_string(self):
        r = self._make_result(verdict="Foca")
        assert r.verdict == "Foca"

    def test_tldr_field_defaults_empty(self):
        r = self._make_result()
        assert r.tldr == ""

    def test_next_step_field_defaults_empty(self):
        r = self._make_result()
        assert r.next_step == ""

    def test_read_min_field_defaults_zero(self):
        r = self._make_result()
        assert r.read_min == 0


# ── Synthesizer.synthesize() popula veredito ─────────────────────────────────

def _make_ranked(score: float, source: str = "github", title: str = "Proj") -> RankedResult:
    return RankedResult(
        source=source,
        title=title,
        url=f"https://github.com/test/{title.lower().replace(' ', '-')}",
        description=f"Description of {title} with enough content to test scoring.",
        metrics={"stars": 5000} if source == "github" else {},
        score=score,
        score_breakdown={"base_score": score},
    )


@pytest.mark.asyncio
async def test_synthesizer_populates_verdict_foca():
    synth = Synthesizer()
    results = await synth.synthesize([_make_ranked(80.0, title="HighScore")])
    assert len(results) > 0
    top = results[0]
    assert top.verdict == "Foca"


@pytest.mark.asyncio
async def test_synthesizer_populates_verdict_ignora():
    synth = Synthesizer()
    results = await synth.synthesize([_make_ranked(10.0, title="LowScore")])
    assert len(results) > 0
    assert results[0].verdict == "Ignora"


@pytest.mark.asyncio
async def test_synthesizer_populates_tldr():
    synth = Synthesizer()
    results = await synth.synthesize([_make_ranked(80.0, title="TldrTest")])
    assert len(results) > 0
    assert len(results[0].tldr) > 0


@pytest.mark.asyncio
async def test_synthesizer_populates_next_step():
    synth = Synthesizer()
    results = await synth.synthesize([_make_ranked(80.0, title="NextStepTest")])
    assert len(results) > 0
    assert len(results[0].next_step) > 0


@pytest.mark.asyncio
async def test_synthesizer_populates_read_min():
    synth = Synthesizer()
    results = await synth.synthesize([_make_ranked(80.0, title="ReadMinTest")])
    assert len(results) > 0
    assert results[0].read_min >= 2


def test_compute_verdict_all_four_verdicts_possible():
    """Verifica que _compute_verdict pode retornar todos os 4 vereditos."""
    verdicts = set()
    for score in [80.0, 60.0, 40.0, 10.0]:
        verdict, _, _, _ = Synthesizer._compute_verdict(score, "desc", [])
        verdicts.add(verdict)
    assert "Foca" in verdicts
    assert "Considera" in verdicts
    assert "Acompanha" in verdicts
    assert "Ignora" in verdicts


# ── report_generator usa verdict ─────────────────────────────────────────────

def test_report_generator_includes_verdict_label():
    from src.report_generator import ReportGenerator
    from src.types import ResearchMetadata

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="resumo executivo gerado")

    rg = ReportGenerator(mock_llm)

    result = SynthesizedResult(
        entity="twenty",
        title="Twenty CRM",
        description="Modern open source CRM",
        sources=["github"],
        urls=["https://github.com/twentyhq/twenty"],
        combined_score=80.0,
        metrics={"stars": 5000},
        highlights=["5000 stars no GitHub"],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
        verdict="Foca",
        tldr="CRM open source moderno com 5k stars — substitui HubSpot.",
        next_step="Avaliar esta semana.",
        read_min=3,
    )

    metadata = ResearchMetadata(
        query="CRM open source",
        domain="saas_b2b",
        sources=["github"],
        total_results=1,
        iterations=1,
        timestamp=datetime.now(),
        duration_seconds=5.0,
    )

    report = rg._assemble_report(
        query="CRM open source",
        metadata=metadata,
        results=[result],
        executive_summary="Resumo.",
        recommendation="Rec.",
        trends="Trends.",
    )

    assert "Foca" in report
    assert "~3 min" in report
    assert "CRM open source moderno" in report
    assert "Avaliar esta semana" in report
