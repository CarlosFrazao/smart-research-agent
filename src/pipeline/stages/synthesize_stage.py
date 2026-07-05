"""Synthesize Stage — Stage independente para consolidação, deduplicação fuzzy e geração do Grafo de Evidências.

Responsabilidades:
  - Deduplicar resultados utilizando FuzzyDeduplicator e o utilitário Deduplicator.
  - Clusterizar semanticamente resultados por entidade com fallback léxico.
  - Consolidar clusters em instâncias de SynthesizedResult.
  - Construir o EvidenceGraph (Grafo de Evidências) cruzando as claims e contradições.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.synthesizer import Synthesizer
from src.evidence_graph import EvidenceGraph
from src.types import RankedResult

logger = logging.getLogger("pipeline.synthesize_stage")


class SynthesizeStage(PipelineStage):
    """Stage independente de síntese, clusterização e geração do grafo de evidências.

    Delega a deduplicação e clusterização para a classe `Synthesizer` e constrói
    o grafo de claims cruzadas usando `EvidenceGraph`.
    """

    name = "synthesize"

    def __init__(
        self,
        synthesizer: Optional[Synthesizer] = None,
        confirm_threshold: float = 0.50,
        contradict_threshold: float = 0.35,
    ):
        self.synthesizer = synthesizer or Synthesizer()
        self.confirm_threshold = confirm_threshold
        self.contradict_threshold = contradict_threshold

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa a consolidação de resultados e constrói o Grafo de Evidências."""
        results: List[RankedResult] = context.ranked_results or []

        if context.metadata is None:
            context.metadata = {}

        logger.info(
            f"SynthesizeStage: iniciando síntese de {len(results)} resultados ranqueados."
        )
        start_time = time.monotonic()

        # 1. Executa deduplicação e clusterização via Synthesizer
        synthesized = await self.synthesizer.synthesize(results)
        duration = time.monotonic() - start_time

        # 2. Constrói o Grafo de Evidências
        evidence_graph = EvidenceGraph(
            confirm_threshold=self.confirm_threshold,
            contradict_threshold=self.contradict_threshold,
        )
        # O build_from_results aceita list[SearchResult]. Como RankedResult herda de
        # SearchResult, podemos passá-los diretamente.
        evidence_graph.build_from_results(results)

        # 3. Atualiza o contexto
        context.synthesized_results = synthesized
        context.set("evidence_graph", evidence_graph)

        # 4. Registra metadados e logs
        total_claims = len(evidence_graph.claims)
        total_relations = len(evidence_graph.relations)

        context.metadata["synthesize"] = {
            "duration_seconds": round(duration, 3),
            "input_results": len(results),
            "synthesized_entities": len(synthesized),
            "evidence_graph_claims": total_claims,
            "evidence_graph_relations": total_relations,
        }

        logger.info(
            f"SynthesizeStage concluído: {len(synthesized)} entidades geradas, "
            f"{total_claims} claims com {total_relations} relações identificadas no grafo."
        )
        return context
