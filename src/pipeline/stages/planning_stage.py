"""PlanningStage — Generate a source plan from the analyzed intent.

Delega a `SourcePlanner` para expandir o `IntentResult` em um
`SourcePlan` (keywords, domínios, sources pesquisáveis). O plano resultante
é injetado em `context.source_plan` para consumo posterior pela `SearchStage`.

Esta stage é não-crítica: falhas são logadas e o pipeline prossegue com o
fallback de busca genérica.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("pipeline.planning_stage")


class PlanningStage(PipelineStage):
    """Gera um plano de fontes (source_plan) baseado no intent da query.

    Args:
        source_planner: Instância de `SourcePlanner`. Se None, é resolvido
            do orchestrator injetado via context.extras.
    """

    name = "planning"
    critical = False

    def __init__(self, source_planner: Any | None = None) -> None:
        self.source_planner = source_planner

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Gera o source_plan e injeta em `context.source_plan`."""
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        intent = context.intent

        if intent is None:
            logger.info("PlanningStage: intent não disponível; pulando.")
            return context

        source_planner = self.source_planner
        if source_planner is None and orchestrator:
            source_planner = getattr(orchestrator, "source_planner", None)

        if source_planner is None:
            logger.info("PlanningStage: SourcePlanner não disponível; pulando.")
            return context

        try:
            plan = await source_planner.plan(intent=intent, query=context.query)
        except Exception as e:
            logger.warning(f"PlanningStage: falha ao gerar source plan: {e}")
            return context

        context.source_plan = plan
        sources = getattr(plan, "sources", [])
        logger.info(
            f"PlanningStage: source_plan gerado com {len(sources)} fontes. "
            f"Primeiras: {', '.join(getattr(s, 'name', str(s)) for s in sources[:5])}."
        )
        return context
