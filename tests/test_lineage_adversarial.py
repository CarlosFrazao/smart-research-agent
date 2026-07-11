"""Testes unitários da Fase 3 — Linhagem de Citação + Passada Adversarial + Confiança.

Cobrem:
1. LineageStage classifica o resultado mais antigo de um cluster como "primary".
2. Resultado que contém o domínio do primário no texto recebe "derivative" e
   cites_within_cluster populado.
3. AdversarialPassStage NÃO roda em modo "guerrilha" (enable_adversarial_pass=False).
4. AdversarialPassStage roda em modo "cirurgia" e injeta resultados com
   is_adversarial=True.
5. _build_confidence_section gera a seção quando há claims com lineage_role=="unknown".

Usa objetos reais do domínio (RankedResult, PipelineContext) e mocks leves para
LLM/SearchStage, sem tocar em rede ou LLM reais.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.operation_modes import OperationModes
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.adversarial_stage import AdversarialPassStage
from src.pipeline.stages.lineage_stage import LineageStage
from src.pipeline.stages.report_stage import ReportStage
from src.types import RankedResult, generate_result_id


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_ranked(
    source: str,
    title: str,
    url: str,
    published_at: datetime | None = None,
    cluster_id: str | None = None,
    description: str = "",
) -> RankedResult:
    r = RankedResult(
        source=source,
        title=title,
        url=url,
        description=description,
        published_at=published_at,
        cluster_id=cluster_id,
    )
    # O pipeline real sempre gera result_id canônico (Fase 1); replicamos aqui.
    r.result_id = generate_result_id(source, url)
    return r


def _ctx_with(ranked: list) -> PipelineContext:
    ctx = PipelineContext(query="test query")
    ctx.ranked_results = ranked
    return ctx


def _ctx_with_mode(mode_name: str) -> PipelineContext:
    """PipelineContext com um orchestrator stub que expõe o OperationConfig."""
    ctx = PipelineContext(query="test query")
    orch = MagicMock()
    orch.operation_mode = OperationModes.get_mode(mode_name)
    ctx.extras["orchestrator"] = orch
    return ctx


# ── 1. Lineage: primary = mais antigo ───────────────────────────────────────

def test_lineage_marks_oldest_as_primary():
    old = _make_ranked(
        "agency", "Original story", "https://agency.example/a",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cluster_id="cluster_1",
    )
    new = _make_ranked(
        "blog", "Repost", "https://blog.example/b",
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cluster_id="cluster_1",
    )
    ctx = _ctx_with([old, new])

    stage = LineageStage()
    import asyncio
    asyncio.run(stage.run(ctx))

    assert old.lineage_role == "primary"
    assert new.lineage_role == "derivative"


# ── 2. Lineage: derivative que cita o primário → cites_within_cluster ─────────

def test_lineage_detects_citation_within_cluster():
    primary = _make_ranked(
        "agency", "Original", "https://agency.example/original",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cluster_id="cluster_2",
    )
    derivative = _make_ranked(
        "blog", "Repost of original", "https://blog.example/repost",
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cluster_id="cluster_2",
        description="Veja a cobertura em https://agency.example/original para mais detalhes.",
    )
    ctx = _ctx_with([primary, derivative])

    stage = LineageStage()
    import asyncio
    asyncio.run(stage.run(ctx))

    assert derivative.lineage_role == "derivative"
    assert primary.result_id in derivative.cites_within_cluster


# ── 3. Adversarial desligado em guerrilha ────────────────────────────────────

def test_adversarial_skipped_in_guerrilha():
    ctx = _ctx_with_mode("guerrilha")
    ctx.ranked_results = [_make_ranked("web", "x", "https://x.example")]

    llm = MagicMock()
    llm.complete = AsyncMock(return_value="problemas com test query")
    search_stage = MagicMock()

    stage = AdversarialPassStage(llm_client=llm, search_stage=search_stage)
    import asyncio
    asyncio.run(stage.run(ctx))

    # Nenhuma busca adversarial deve ter sido disparada.
    search_stage.run.assert_not_called()
    llm.complete.assert_not_called()
    assert ctx.get("adversarial_hits", 0) == 0


# ── 4. Adversarial ligado em cirurgia → injeta is_adversarial=True ────────────

def test_adversarial_runs_in_cirurgia_and_injects_flag():
    ctx = _ctx_with_mode("cirurgia")
    ctx.ranked_results = [_make_ranked("web", "x", "https://x.example")]

    llm = MagicMock()
    llm.complete = AsyncMock(return_value="críticas a test query")

    # SearchStage stub: comporta-se como a implementação real — MUTA o
    # contexto efêmero recebido e popula ranked_results (e o retorna).
    adv_result = _make_ranked("web", "counterpoint", "https://counter.example")

    def _fake_run(adv_ctx):
        adv_ctx.ranked_results = [adv_result]
        return adv_ctx

    search_stage = MagicMock()
    search_stage.run = AsyncMock(side_effect=_fake_run)

    stage = AdversarialPassStage(llm_client=llm, search_stage=search_stage)
    import asyncio
    asyncio.run(stage.run(ctx))

    search_stage.run.assert_awaited_once()
    # O resultado adversarial deve ter sido marcado e anexado ao contexto.
    assert any(getattr(r, "is_adversarial", False) for r in ctx.ranked_results)
    assert ctx.get("adversarial_hits") == 1


# ── 5. Confidence section com lineage_role == "unknown" ──────────────────────

def test_confidence_section_for_unknown_lineage():
    unknown = _make_ranked("web", "unverified claim", "https://unverified.example")
    # Sem cluster_id => lineage_role permanece "unknown" (default).
    section = ReportStage._build_confidence_section([unknown])

    assert "## ⚠️ Nível de Confiança por Afirmação" in section
    assert "unverified claim" in section


def test_confidence_section_empty_when_all_confirmed():
    primary = _make_ranked(
        "agency", "Verified", "https://agency.example/v",
        cluster_id="c",
    )
    primary.lineage_role = "primary"
    section = ReportStage._build_confidence_section([primary])
    assert section == ""
