"""src/pipeline/stages — Stages concretas do domínio de pesquisa do SRA.

Cada módulo aqui implementa `PipelineStage` (`src/pipeline/pipeline.py`)
delegando a um serviço/analyzer já existente no codebase.
"""

import logging

logger = logging.getLogger(__name__)

from src.pipeline.stages.intent_stage import IntentStage
from src.pipeline.stages.storm_stage import StormStage
from src.pipeline.stages.expand_stage import ExpandStage
from src.pipeline.stages.search_stage import SearchStage
from src.pipeline.stages.rank_stage import RankStage
from src.pipeline.stages.score_stage import ScoreStage
from src.pipeline.stages.verification_stage import VerificationStage
from src.pipeline.stages.graph_explorer_stage import GraphExplorerStage
from src.pipeline.stages.synthesize_stage import SynthesizeStage
from src.pipeline.pipeline import PipelineStage, PipelineContext

# Mapeamentos e Aliases de compatibilidade para stage_factory.py
RankingStage = RankStage
SynthesisStage = SynthesizeStage


# Stubs de compatibilidade temporária para o pipeline de 9 estágios legado
class HealthCheckStage(PipelineStage):
    name = "health_check"

    async def run(self, context: PipelineContext) -> None:
        if hasattr(self.orchestrator, "_health_check"):
            await self.orchestrator._health_check()


class PlanningStage(PipelineStage):
    name = "planning"

    async def run(self, context: PipelineContext) -> None:
        pass


class ConflictResolutionStage(PipelineStage):
    name = "conflict_resolution"

    async def run(self, context: PipelineContext) -> None:
        pass


class GapFillStage(PipelineStage):
    name = "gap"

    async def run(self, context: PipelineContext) -> None:
        pass


class SanitizationStage(PipelineStage):
    name = "sanitization"
    critical = False

    def __init__(self, sanitizer=None):
        self.sanitizer = sanitizer

    async def run(self, context: PipelineContext) -> None:
        """Sanitiza os resultados de busca usando LLMSanitizer.

        Aplica sanitização de prompt injection nas descrições dos resultados
        de busca brutos. Falhas são não-críticas: o pipeline continua mesmo
        se a sanitização falhar.
        """
        from src.security.llm_sanitizer import LLMSanitizer

        results = getattr(context, "search_results", None) or getattr(
            context, "raw_results", None
        )
        if not results:
            logger.info("SanitizationStage: sem resultados para sanitizar")
            return

        # Obtém o sanitizer do contexto ou cria um novo
        sanitizer = self.sanitizer
        if sanitizer is None:
            orchestrator = (
                context.extras.get("orchestrator") if context.extras else None
            )
            if orchestrator and hasattr(orchestrator, "sanitizer"):
                sanitizer = orchestrator.sanitizer

        if sanitizer is None:
            logger.debug("SanitizationStage: LLMSanitizer não disponível")
            return

        sanitized_count = 0
        for result in results:
            desc = getattr(result, "description", "") or ""
            if not desc or len(desc) < 100:
                continue  # Texto curto: pulado (conforme LLMSanitizer)
            try:
                sanitized = await sanitizer.sanitize(desc)
                if sanitized.was_injection_detected:
                    logger.warning(
                        "[SEGURANÇA] Prompt injection detectado em '%s' URL=%s",
                        getattr(result, "source", "unknown"),
                        getattr(result, "url", ""),
                    )
                if hasattr(result, "description"):
                    result.description = sanitized.cleaned
                    sanitized_count += 1
            except Exception as e:
                logger.warning(f"SanitizationStage: falha ao sanitizar resultado: {e}")

        if sanitized_count:
            logger.info(
                f"SanitizationStage: {sanitized_count} resultado(s) sanitizado(s)"
            )


from src.pipeline.stages.report_stage import ReportStage

__all__ = [
    "IntentStage",
    "StormStage",
    "ExpandStage",
    "SearchStage",
    "RankingStage",
    "RankStage",
    "ScoreStage",
    "GraphExplorerStage",
    "SynthesisStage",
    "SynthesizeStage",
    "HealthCheckStage",
    "PlanningStage",
    "ConflictResolutionStage",
    "GapFillStage",
    "SanitizationStage",
    "ReportStage",
]
