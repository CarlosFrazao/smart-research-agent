"""ConflictResolutionStage — Detecta e registra conflitos entre claims.

Delega a `ConflictDetector` para analisar as claims em
`context.ranked_results` e injetar os conflitos detectados em
`context.extra["conflict_report"]`.

Esta stage é não-crítica: falhas são logadas e o pipeline prossegue.
Ela não resolve conflitos automaticamente — apenas os reporta para que
estágios posteriores (SynthesizeStage, ReportStage) possam considerá-los.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("pipeline.conflict_resolution_stage")


class ConflictResolutionStage(PipelineStage):
    """Detecta e registra conflitos entre claims nos ranked_results.

    Args:
        conflict_detector: Instância de `ConflictDetector`. Se None, é resolvido
            do orchestrator injetado via context.extras.
    """

    name = "conflict_resolution"
    critical = False

    def __init__(self, conflict_detector: Any | None = None) -> None:
        self.conflict_detector = conflict_detector

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Detecta conflitos e injeta em `context.extra["conflict_report"]`."""
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        conflict_detector = (
            self.conflict_detector
            if self.conflict_detector is not None
            else getattr(orchestrator, "conflict_detector", None)
            if orchestrator
            else None
        )

        if conflict_detector is None:
            logger.info(
                "ConflictResolutionStage: ConflictDetector não disponível; pulando."
            )
            return context

        if not context.ranked_results:
            logger.info("ConflictResolutionStage: sem ranked_results; pulando.")
            return context

        try:
            report = conflict_detector.detect(context.ranked_results)
        except Exception as e:
            logger.warning(f"ConflictResolutionStage: falha ao detectar conflitos: {e}")
            return context

        context.extra["conflict_report"] = report
        conflict_count = len(report.conflicts) if hasattr(report, "conflicts") else 0
        resolution_count = (
            len(report.resolutions) if hasattr(report, "resolutions") else 0
        )
        logger.info(
            f"ConflictResolutionStage: {conflict_count} conflito(s) detectado(s), "
            f"{resolution_count} resolução(ões) sugerida(s)."
        )
        return context
