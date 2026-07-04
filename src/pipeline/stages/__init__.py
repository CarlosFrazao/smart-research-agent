"""src/pipeline/stages — Stages concretas do domínio de pesquisa do SRA.

Cada módulo aqui implementa `PipelineStage` (`src/pipeline/pipeline.py`)
delegando a um serviço/analyzer já existente no codebase.
"""

from src.pipeline.stages.intent_stage import IntentStage
from src.pipeline.stages.expand_stage import ExpandStage
from src.pipeline.stages.search_stage import SearchStage
from src.pipeline.stages.rank_stage import RankStage
from src.pipeline.stages.score_stage import ScoreStage
from src.pipeline.stages.synthesize_stage import SynthesizeStage
from src.pipeline.pipeline import PipelineStage, PipelineContext

# Mapeamentos e Aliases de compatibilidade para stage_factory.py
RankingStage = RankStage
SynthesisStage = SynthesizeStage

# Stubs de compatibilidade temporária para o pipeline de 9 estágios legado
class HealthCheckStage(PipelineStage):
    async def run(self, context: PipelineContext) -> None:
        if hasattr(self.orchestrator, "_health_check"):
            await self.orchestrator._health_check()

class PlanningStage(PipelineStage):
    async def run(self, context: PipelineContext) -> None:
        pass

class ConflictResolutionStage(PipelineStage):
    async def run(self, context: PipelineContext) -> None:
        pass

class GapFillStage(PipelineStage):
    async def run(self, context: PipelineContext) -> None:
        pass

class SanitizationStage(PipelineStage):
    async def run(self, context: PipelineContext) -> None:
        pass

from src.pipeline.stages.report_stage import ReportStage

__all__ = [
    "IntentStage",
    "ExpandStage",
    "SearchStage",
    "RankingStage",
    "RankStage",
    "ScoreStage",
    "SynthesisStage",
    "SynthesizeStage",
    "HealthCheckStage",
    "PlanningStage",
    "ConflictResolutionStage",
    "GapFillStage",
    "SanitizationStage",
    "ReportStage",
]
