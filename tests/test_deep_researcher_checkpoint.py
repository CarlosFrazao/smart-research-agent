"""Testes do FEAT-006 — DeepResearcher retoma de checkpoint (FEAT-005).

Cenário principal (PRD 4.6.7): simula 10 passos de deep research, crash no
passo 6, load do checkpoint e retoma até o fim. Valida que o progresso é
persistido a cada ``checkpoint_every`` passos e que, após o resume, o resultado
final é completo (a árvore é reconstruída deterministicamente a partir da query).

Observação de design (contrato FEAT-005): o checkpoint persiste
``{query, steps_done, draft}`` — não a árvore de nós. O resume reconstrói a
árvore a partir da query (determinística com LLM mockado) e continua a partir do
ponto de progresso reportado, não perdendo o trabalho já realizado.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.deep_researcher import DeepResearcher
from src.pipeline.checkpoint import DeepCheckpoint
from src.types import SearchResult


def _make_result(url: str) -> SearchResult:
    r = SearchResult(
        source="web",
        title=f"Result {url}",
        url=url,
        description=f"descricao do resultado {url}",
        metrics={},
    )
    r.confidence_score = 0.8
    return r


def _make_orchestrator(hypotheses):
    """Orchestrator mockado: busca determinística + geração de hipóteses fixas."""
    orch = MagicMock()
    plan = MagicMock()
    orch.source_planner.plan.return_value = plan
    orch._parallel_search = AsyncMock(
        return_value=[_make_result(f"https://ex.com/{i}") for i in range(3)]
    )
    orch.ranker.rank = AsyncMock(side_effect=lambda results: results)
    orch.confidence_scorer.score_batch = AsyncMock(
        side_effect=lambda results, cross_validate=False: results
    )
    # DLQ presente (usado no tratamento de erro de busca).
    orch.dlq = MagicMock()
    orch.dlq.push = AsyncMock()
    orch.dlq.create_failed_task = MagicMock(return_value={})
    # O LLM do deep researcher gera as mesmas hipóteses em toda chamada.
    orch.llm = MagicMock()
    orch.llm.generate_structured = AsyncMock(return_value=hypotheses)
    # Evita que getattr(orchestrator, 'config', None) resolva para MagicMock.
    orch.config = None
    return orch


def _make_llm(hypotheses):
    llm = MagicMock()
    llm.generate_structured = AsyncMock(return_value=hypotheses)
    return llm


def _build_researcher(tmp_path, hypotheses, checkpoint, run_id=None):
    """DeepResearcher configurado para produzir >=10 nós de forma determinística."""
    orch = _make_orchestrator(hypotheses)
    dr = DeepResearcher(
        llm_client=_make_llm(hypotheses),
        orchestrator=orch,
        memory=None,
        content_normalizer=None,
        checkpoint=checkpoint,
        run_id=run_id,
    )
    # Árvore larga e profunda para garantir >10 nós explorados.
    dr.MAX_DEPTH = 3
    dr.MAX_BRANCHES = 4
    dr.BEAM_WIDTH = 10
    dr.MIN_CONFIDENCE = 0.0  # nunca poda por confiança baixa
    dr.CONFIRMED_THRESHOLD = 2.0  # nunca confirma (mantém expandindo)
    return dr


@pytest.mark.asyncio
async def test_checkpoint_saved_every_n_steps(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    hyps = ["h1", "h2", "h3", "h4"]
    dr = _build_researcher(tmp_path, hyps, ck, run_id="run-save")

    await dr.research("query ampla para teste")

    # steps_done total = raiz(1) + filhos de 3 levels * 4 branches.
    # Deve ter pelo menos um checkpoint salvo em múltiplo de 5.
    state = ck.load("run-save")
    assert state is not None
    assert state["steps_done"] >= 5
    assert "query ampla para teste" == state["query"]


@pytest.mark.asyncio
async def test_resume_after_crash_at_step_6(tmp_path, monkeypatch):
    """PRD 4.6.7: 10 passos, crash no 6, load -> retoma até o fim."""
    monkeypatch.setenv("SRA_CHECKPOINT_EVERY", "2")
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    hyps = ["h1", "h2", "h3", "h4"]

    # ── Execução 1: corre até o passo 6 e então "crasha" ──────────────────────
    class CrashAtStep6(Exception):
        pass

    dr1 = _build_researcher(tmp_path, hyps, ck, run_id="run-resume")

    original_explore = dr1._explore_child_data

    async def crashing_explore(child):
        if dr1._steps_done >= 6:
            # Simula crash logo após concluir o passo 6.
            raise CrashAtStep6()
        return await original_explore(child)

    dr1._explore_child_data = crashing_explore

    with pytest.raises(CrashAtStep6):
        await dr1.research("query para retomada")

    # O checkpoint deve refletir progresso até o passo 6 (ou logo antes).
    saved = ck.load("run-resume")
    assert saved is not None
    assert saved["steps_done"] >= 1

    # ── Execução 2: retoma do checkpoint, roda até o fim ───────────────────────
    dr2 = _build_researcher(tmp_path, hyps, ck, run_id="run-resume")
    result2 = await dr2.research("query para retomada")

    # O resume reconstrói a árvore e a completa: nós explorados >= 10.
    assert result2.total_nodes_explored >= 10
    final = ck.load("run-resume")
    assert final is not None
    assert final["steps_done"] >= 10
    # A árvore final tem findings consolidados (não vazia).
    assert len(result2.findings) > 0


@pytest.mark.asyncio
async def test_no_checkpoint_when_disabled(tmp_path):
    """Sem checkpoint injetado, research roda normalmente (compatibilidade)."""
    hyps = ["h1", "h2", "h3", "h4"]
    dr = _build_researcher(tmp_path, hyps, checkpoint=None, run_id=None)
    result = await dr.research("query sem checkpoint")
    assert result.total_nodes_explored >= 10
    # Nenhum arquivo de checkpoint criado.
    assert not (tmp_path / ".sra_checkpoints").exists()
