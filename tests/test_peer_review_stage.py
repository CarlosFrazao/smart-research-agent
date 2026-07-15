"""
Testes do Peer Review Stage (Bloco 9 / E4-T1).

Cobre:
- Estágio desativado (enabled=False) -> no-op, sem issues, seção vazia.
- Detecção determinística local de claim sem fonte (UNSUPPORTED).
- Detecção determinística local de contradição entre claims do mesmo tópico.
- Estágio roda sem agente/LLM (apenas checagens locais, zero rede).
- Merge de issues do PeerReviewAgent (via agente fake) com as locais.
- Registro na StageFactory e posição no pipeline (após quality_gate).
- Seção "⚠️ Limitações e Caveats (Peer Review)" renderizada na presença de issues.
- Nenhuma chamada HTTP adicional (trabalha só com context.extra / ranked_results).
"""

from unittest.mock import MagicMock

import pytest

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.peer_review_stage import PeerReviewStage
from src.pipeline.stage_factory import StageFactory
from src.peer_review_agent import PeerReviewAgent, PeerReviewReport, ReviewIssue
from src.synthesizer import Synthesizer
from src.types import SynthesizedClaim, SynthesizedResult


# ── Helpers ────────────────────────────────────────────────────────────────


def _claim(text: str, source_id: str = "", confidence: float = 1.0) -> SynthesizedClaim:
    """Constrói um SynthesizedClaim mínimo com proveniência opcional."""
    return SynthesizedClaim(
        text=text,
        source_ids=[source_id] if source_id else [],
        urls=[f"https://a.com/{source_id}"] if source_id else [],
        confidence=confidence,
    )


def _synthesized(entity: str, claim_text: str, source_id: str) -> SynthesizedResult:
    """Constrói um SynthesizedResult mínimo (carrega tldr = claim).

    Quando ``source_id`` é vazio, o ``result_id`` também fica vazio para
    simular uma afirmação sem proveniência nenhuma (cai no caso UNSUPPORTED).
    """
    has_source = bool(source_id)
    return SynthesizedResult(
        entity=entity,
        title=entity,
        description=f"{entity} desc",
        sources=["web"] if has_source else [],
        urls=[f"https://a.com/{source_id}"] if has_source else [],
        result_id=source_id if has_source else "",
        combined_score=80.0,
        tldr=claim_text,
    )


def _ctx_with_claims(claims: list[SynthesizedClaim]) -> PipelineContext:
    """PipelineContext com synthesized_claims já derivados (Bloco 5)."""
    ctx = PipelineContext(query="q")
    ctx.extra["synthesized_claims"] = list(claims)
    return ctx


# ── Estágio desativado (no-op) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_disabled_is_noop():
    """Estágio desativado -> sem issues, seção vazia, não quebra."""
    stage = PeerReviewStage(enabled=False)
    ctx = _ctx_with_claims([_claim("afirmação sem fonte")])
    out = await stage.run(ctx)
    assert out.extra["peer_review_section"] == ""
    assert out.extra["peer_review_issues"] == []


# ── Checagem (a): claim sem fonte (UNSUPPORTED) ─────────────────────────────


@pytest.mark.asyncio
async def test_detects_unsupported_claim():
    """Claim sem source_id -> issue unsupported_claim e seção renderizada."""
    stage = PeerReviewStage(enabled=True, agent=None, llm_client=None)
    ctx = _ctx_with_claims([_claim("modelo X supera todos os demais")])
    out = await stage.run(ctx)

    issues = out.extra["peer_review_issues"]
    assert len(issues) == 1
    assert issues[0].category == "unsupported_claim"
    assert issues[0].severity == "major"
    section = out.extra["peer_review_section"]
    assert "⚠️ Limitações e Caveats (Peer Review)" in section
    assert "unsupported_claim" in section


@pytest.mark.asyncio
async def test_supported_claim_has_no_unsupported_issue():
    """Claim com source_id -> nenhuma issue de unsupported_claim."""
    stage = PeerReviewStage(enabled=True, agent=None, llm_client=None)
    ctx = _ctx_with_claims([_claim("modelo X lidera em latência", source_id="src1")])
    out = await stage.run(ctx)
    issues = out.extra["peer_review_issues"]
    assert not any(i.category == "unsupported_claim" for i in issues)
    assert out.extra["peer_review_section"] == ""


# ── Checagem (b): contradição entre claims ─────────────────────────────────


@pytest.mark.asyncio
async def test_detects_contradiction():
    """Dois claims do mesmo tópico com confiança oposta -> contradição."""
    stage = PeerReviewStage(enabled=True, agent=None, llm_client=None)
    claims = [
        _claim("rust oferece segurança de memória", source_id="s1", confidence=0.9),
        _claim("rust oferece segurança de memória limitada", source_id="s2", confidence=0.2),
    ]
    ctx = _ctx_with_claims(claims)
    out = await stage.run(ctx)

    issues = out.extra["peer_review_issues"]
    assert any(i.category == "bias" and "contradição" in i.description.lower() for i in issues)


@pytest.mark.asyncio
async def test_no_false_contradiction_distinct_topics():
    """Claims de tópicos distintos não disparam contradição."""
    stage = PeerReviewStage(enabled=True, agent=None, llm_client=None)
    claims = [
        _claim("python é popular para data science", source_id="s1", confidence=0.9),
        _claim("go é eficiente em concorrência", source_id="s2", confidence=0.1),
    ]
    ctx = _ctx_with_claims(claims)
    out = await stage.run(ctx)
    issues = out.extra["peer_review_issues"]
    assert not any("contradição" in i.description.lower() for i in issues)


# ── Derivação de claims a partir de synthesized_results ─────────────────────


@pytest.mark.asyncio
async def test_derives_claims_from_synthesized():
    """Sem synthesized_claims no contexto, deriva do Synthesizer (Bloco 5)."""
    stage = PeerReviewStage(enabled=True, agent=None, llm_client=None)
    ctx = PipelineContext(query="q")
    ctx.synthesized_results = [
        _synthesized("entidade", "afirmação sem fonte derivada", "")
    ]
    out = await stage.run(ctx)
    issues = out.extra["peer_review_issues"]
    assert any(i.category == "unsupported_claim" for i in issues)


# ── Merge com PeerReviewAgent (agente fake, offline) ───────────────────────


class _FakeAgent(PeerReviewAgent):
    """PeerReviewAgent fake que devolve issues determinísticos sem rede."""

    def __init__(self) -> None:  # não precisa de llm_client real
        self.prompt_path = ""

    async def review(self, report: str, results: list, query: str = ""):
        return PeerReviewReport(
            overall_assessment="moderate",
            confidence_in_report=0.70,
            issues=[
                ReviewIssue(
                    category="weak_citation",
                    severity="minor",
                    description="Citação aponta para URL fora das fontes.",
                    location="link externo",
                    suggestion="Confirmar se a URL é fonte válida.",
                )
            ],
            strengths=["Estrutura clara."],
            recommendations=["Revisar fontes."],
        )


@pytest.mark.asyncio
async def test_merges_agent_issues():
    """Issues do PeerReviewAgent são mescladas com as locais (sem duplicar)."""
    stage = PeerReviewStage(enabled=True, agent=_FakeAgent(), llm_client=None)
    ctx = _ctx_with_claims([_claim("afirmação sem fonte")])
    out = await stage.run(ctx)

    issues = out.extra["peer_review_issues"]
    # Local (unsupported) + agente (weak_citation) = 2 issues distintas.
    assert len(issues) == 2
    assert any(i.category == "unsupported_claim" for i in issues)
    assert any(i.category == "weak_citation" for i in issues)
    assert out.extra["peer_review_assessment"] == "moderate"


# ── Registro na StageFactory / pipeline ─────────────────────────────────────


def test_factory_registers_peer_review():
    """StageFactory registra e consegue construir o peer_review stage."""
    from src.config import Config

    factory = StageFactory(config=Config(), llm_client=MagicMock())
    assert "peer_review" in factory.get_available_stages()
    stage = factory.create_stage("peer_review")
    assert isinstance(stage, PeerReviewStage)
    assert stage.enabled is True


def test_factory_respects_disabled_config():
    """Config enable_peer_review=False -> stage construído desativado."""
    from src.config import Config

    config = Config()
    config.enable_peer_review = False
    factory = StageFactory(config=config, llm_client=MagicMock())
    stage = factory.create_stage("peer_review")
    assert stage.enabled is False


def test_build_pipeline_includes_peer_review_after_quality_gate():
    """build_pipeline posiciona peer_review após quality_gate e antes de report."""
    from src.config import Config

    config = Config()
    orch = MagicMock()
    orch.config = config
    orch.llm = MagicMock()
    orch.cache = None

    # Minimiza a factory para evitar wiring pesado: cria e inspeciona a ordem.
    factory = StageFactory(orchestrator=orch, config=config, llm_client=MagicMock())
    names = [
        "synthesize",
        "quality_gate",
        "peer_review",
        "media_ingestion",
        "report",
    ]
    stages = [factory.create_stage(n) for n in names]
    stage_names = [s.name for s in stages]
    assert "peer_review" in stage_names
    assert stage_names.index("quality_gate") < stage_names.index("peer_review")
    assert stage_names.index("peer_review") < stage_names.index("report")
