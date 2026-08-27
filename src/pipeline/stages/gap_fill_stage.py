"""GapFillStage — Detecta lacunas de cobertura e dispara re-busca iterativa.

Esta é a stage **produtora canônica** de `context.gap_analysis`. Ela usa o
`GapDetector` (``src/gap_detector.py``) para analisar os resultados
ranqueados contra o intent da query, detectando aspectos não cobertos e
gerando novas queries de aprofundamento.

Funcionamento:
1. Chama ``gap_detector.detect()`` com os resultados ranqueados, query e intent.
2. Armazena o ``GapAnalysis`` resultante em ``context.extra["gap_analysis"]``
   e o flag ``context.extra["is_complete"]``.
3. Injeta as ``new_queries`` do GapAnalysis em ``context.expanded_queries``
   para consumo pelo loop de verificação no ``ResearchPipeline.run()``
   (ver ``src/pipeline/pipeline.py``, pós-pipeline verification loop).

É uma stage **não-crítica** (``critical = False``): falhas são logadas e
o pipeline prossegue normalmente.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("pipeline.gap_fill_stage")


class GapFillStage(PipelineStage):
    """Stage de detecção e preenchimento de lacunas de cobertura.

    Args:
        gap_detector: Instância de ``GapDetector``. Se ``None``, é resolvido
            do orchestrator injetado via ``context.extras``.
    """

    name = "gap"
    critical = False  # gap fill é best-effort: nunca aborta o pipeline

    def __init__(self, gap_detector: Any | None = None) -> None:
        self.gap_detector = gap_detector

    async def run(self, context: PipelineContext) -> PipelineContext:
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        gap_detector = (
            self.gap_detector
            if self.gap_detector is not None
            else getattr(orchestrator, "gap_detector", None)
            if orchestrator
            else None
        )

        if gap_detector is None:
            logger.info("GapFillStage: GapDetector não disponível; pulando.")
            return context

        try:
            gap_analysis = await gap_detector.detect(
                results=context.ranked_results or [],
                query=context.query,
                intent=context.intent,
            )
        except Exception as e:
            logger.warning(f"GapFillStage: falha ao detectar gaps: {e}")
            return context

        # Armazena o gap_analysis no contexto para consumo posterior
        # (DecisionEngine, ReportStage loop, etc.)
        context.extra["gap_analysis"] = gap_analysis
        context.extra["is_complete"] = getattr(gap_analysis, "is_complete", True)

        # Se houver novas queries de gap, injeta em expanded_queries para re-busca
        new_queries = getattr(gap_analysis, "new_queries", None) or []
        existing_texts = {getattr(q, "query", str(q)) for q in context.expanded_queries}
        appended = 0
        for eq in new_queries:
            if eq.query not in existing_texts:
                context.expanded_queries.append(eq)
                existing_texts.add(eq.query)
                appended += 1

        logger.info(
            f"GapFillStage: detected {len(new_queries)} gap query(s), "
            f"{len(getattr(gap_analysis, 'missing_aspects', []))} missing aspects, "
            f"is_complete={gap_analysis.is_complete}, appended={appended}."
        )

        # O pipeline externo (ResearchPipeline.run()) lerá
        # context.extra["is_complete"] e disparará iteração adicional se
        # is_complete=False e há novas queries.
        return context
