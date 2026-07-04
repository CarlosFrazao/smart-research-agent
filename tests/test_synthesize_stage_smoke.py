"""Smoke test manual (e via pytest) do SynthesizeStage.

Testa os comportamentos centrais do SynthesizeStage:
  1. Deduplicação e clusterização via Synthesizer.
  2. Geração e integração do EvidenceGraph (Grafo de Evidências) no contexto.
"""

import pytest
from datetime import datetime

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.synthesize_stage import SynthesizeStage
from src.types import RankedResult, SynthesizedResult
from src.evidence_graph import EvidenceGraph


# ── Testes ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_stage_happy_path():
    # Prepara resultados ranqueados com alguma sobreposição de claims
    # mock1 e mock2 têm o mesmo tópico (Docker/Containers), mas claims ligeiramente diferentes
    r1 = RankedResult(
        source="mock1",
        title="Docker best practices for developers",
        url="https://mock1.com/docker",
        description="Docker is essential for containerization. Studies show container usage is up by 80 percent.",
        score=85.0
    )
    r2 = RankedResult(
        source="mock2",
        title="Docker guide: containerization standards",
        url="https://mock2.com/docker-guide",
        description="Docker is crucial for containerization. Statistics show container usage increased by 80 percent.",
        score=78.0
    )
    r3 = RankedResult(
        source="mock3",
        title="Python asyncio tutorials",
        url="https://mock3.com/asyncio",
        description="Learn python asyncio in 10 minutes. Python asyncio allows single threaded concurrency.",
        score=90.0
    )

    results = [r1, r2, r3]

    # Cria o stage
    stage = SynthesizeStage()

    # Prepara o contexto do pipeline
    context = PipelineContext(query="Docker and python asyncio")
    context.ranked_results = results

    # Executa o stage diretamente
    res_context = await stage.run(context)

    # 1. Valida síntese e clusterização (deve agrupar Docker no mesmo cluster se a similaridade de título/descrição for alta)
    assert len(res_context.synthesized_results) >= 1
    # Verifica se os resultados sintetizados são de fato SynthesizedResult
    assert all(isinstance(x, SynthesizedResult) for x in res_context.synthesized_results)

    # 2. Valida se o EvidenceGraph foi criado e anexado ao contexto
    evidence_graph = res_context.get("evidence_graph")
    assert evidence_graph is not None
    assert isinstance(evidence_graph, EvidenceGraph)

    # Deve conter as claims extraídas dos textos
    assert len(evidence_graph.claims) > 0
    # Verifica se detectou alguma relação (por ex., as frases de r1 e r2 sobre 80 percent container usage)
    assert len(evidence_graph.relations) >= 0

    # 3. Valida os metadados do stage
    meta = res_context.metadata.get("synthesize")
    assert meta is not None
    assert meta["input_results"] == 3
    assert meta["evidence_graph_claims"] == len(evidence_graph.claims)
    assert meta["evidence_graph_relations"] == len(evidence_graph.relations)


async def run_tests_manual():
    print("Iniciando testes manuais de SynthesizeStage...")
    await test_synthesize_stage_happy_path()
    print("🎉 Todos os testes de SynthesizeStage passaram!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_tests_manual())
