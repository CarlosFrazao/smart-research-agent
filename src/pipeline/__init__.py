"""src/pipeline — Pipeline Pattern para o fluxo de pesquisa do SRA.

Substitui o God Object `Orchestrator` (item 1 do plano de correções) por
uma composição declarável de `PipelineStage`s executadas por
`ResearchPipeline`. Ver `src/pipeline/pipeline.py` para o motor genérico
e `src/pipeline/stages/` (itens 22-29) para as stages concretas do
domínio de pesquisa.
"""

from src.pipeline.pipeline import (
    PipelineContext,
    PipelineError,
    PipelineStage,
    ResearchPipeline,
    StageError,
)

__all__ = [
    "PipelineContext",
    "PipelineError",
    "PipelineStage",
    "ResearchPipeline",
    "StageError",
]
