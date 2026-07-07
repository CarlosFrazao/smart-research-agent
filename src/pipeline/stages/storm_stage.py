"""src/pipeline/stages/storm_stage.py — Stage de perspectivas STORM.

Adaptador fino entre o `ResearchPipeline` (`src/pipeline/pipeline.py`) e
`StormPerspectiveGenerator` (`src/storm_perspectives.py`), que até então só
era exercitado pelos próprios testes unitários (`tests/test_storm_perspectives.py`)
e não era chamado por nenhum stage nem pelo `DeepResearcher`.

Papel no pipeline
-----------------
Roda logo após `IntentStage` e antes de `ExpandStage`: simula um painel de
especialistas (estilo STORM — "multi-perspective question asking") sobre o
tópico da query, ANTES da expansão lexical feita pelo `QueryExpander` e antes
da árvore de busca do `DeepResearcher`. É ortogonal a ambos:

- `QueryExpander` (via `ExpandStage`) gera variações lexicais/de tipo de UMA
  query.
- `StormStage` gera múltiplas personas com sub-queries direcionadas por
  ângulo, a partir do tópico.

Por que `critical = False`
--------------------------
`StormPerspectiveGenerator.generate_perspectives_with_queries()` já é
resiliente por construção (fallback determinístico para 3 personas fixas se
o LLM falhar ou devolver lixo) — na prática esta stage não deveria lançar.
Mesmo assim, tratamos como enriquecimento best-effort (mesma categoria de
`EvidenceGraph`/`PeerReview`): se algo inesperado acontecer (bug, erro de
serialização, etc.), a falha é registrada em `context.errors` e o pipeline
segue para `ExpandStage` normalmente, em vez de abortar uma etapa que é
puramente aditiva.

O que esta stage popula
------------------------
Não sobrescreve `context.expanded_queries` (campo "de propriedade" do
`ExpandStage`, que o recalcula do zero a cada execução — ver
`ExpandStage.run`). Em vez disso, grava em `context.extra` (via
`context.set`/`context.get`, helpers já existentes em `PipelineContext`):

- ``context.extra["storm_perspectives"]``: saída estruturada completa
  (lista de dicts com ``name``/``description``/``sub_queries``), útil para
  telemetria, relatório ou futura injeção manual em `DeepResearcher`.
- ``context.extra["storm_seed_queries"]``: lista achatada e deduplicada de
  todas as `sub_queries` de todas as personas — pronta para um `ExpandStage`
  ou `SearchStage` futuro consumir como seed adicional (hoje nenhum dos dois
  lê essa chave; a integração de fato é um passo separado e deliberadamente
  fora do escopo deste stage, para não acoplar/alterar o contrato já
  existente de `ExpandStage` sem necessidade).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.storm_perspectives import StormPerspectiveGenerator

if TYPE_CHECKING:  # pragma: no cover - apenas para type hints
    from src.cache import Cache
    from src.clients.llm_client import LLMClient

logger = logging.getLogger("pipeline.storm_stage")

__all__ = ["StormStage"]


class StormStage(PipelineStage):
    """Gera perspectivas de especialistas (STORM) para a query/tópico atual.

    Popula:
        - ``context.extra["storm_perspectives"]``: saída completa do
          `StormPerspectiveGenerator` (personas + sub-queries).
        - ``context.extra["storm_seed_queries"]``: sub-queries achatadas e
          deduplicadas de todas as personas.
    """

    name = "storm"
    critical = False

    def __init__(
        self,
        storm_generator: Optional[StormPerspectiveGenerator] = None,
        *,
        llm_client: Optional["LLMClient"] = None,
        cache: Optional["Cache"] = None,
        num_perspectives: int = 3,
        enabled: bool = True,
    ) -> None:
        """
        Args:
            storm_generator: Instância já pronta de `StormPerspectiveGenerator`.
                Tem prioridade sobre `llm_client`/`cache` se fornecida
                (útil para testes/mocking, no mesmo espírito de
                `ExpandStage(query_expander=...)`).
            llm_client: `LLMClient` compartilhado do container de
                dependências (injetado pela `StageFactory`). Usado para
                construir um `StormPerspectiveGenerator` internamente caso
                `storm_generator` não seja passado.
            cache: `Cache` compartilhado (SHA-256, mesma instância usada por
                `ExpandStage`/`ReportStage`) para evitar chamadas LLM
                redundantes ao gerar perspectivas para o mesmo tópico
                (`StormPerspectiveGenerator` já cacheia internamente quando
                recebe um `cache`).
            num_perspectives: Quantas personas gerar (repassado a
                `generate_perspectives_with_queries`).
            enabled: Permite desligar o stage via configuração sem removê-lo
                do pipeline (equivalente a um no-op quando `False`).
        """
        if storm_generator is not None:
            self.storm_generator: Optional[StormPerspectiveGenerator] = storm_generator
        elif llm_client is not None:
            self.storm_generator = StormPerspectiveGenerator(
                llm_client=llm_client, cache=cache
            )
        else:
            # Sem generator nem llm_client: stage vira no-op defensivo em vez
            # de quebrar a construção do pipeline (ver `run`).
            self.storm_generator = None

        self.num_perspectives = num_perspectives
        self.enabled = enabled

    async def run(self, context: PipelineContext) -> PipelineContext:
        if not self.enabled:
            logger.info("storm_stage.disabled")
            return context

        if self.storm_generator is None:
            logger.warning(
                "storm_stage.no_generator: nenhum StormPerspectiveGenerator "
                "nem LLMClient foram injetados; pulando geração de perspectivas."
            )
            return context

        topic = context.enriched_query or context.query
        logger.info(f"StormStage: gerando perspectivas para — '{topic[:80]}'")

        try:
            perspectives = (
                await self.storm_generator.generate_perspectives_with_queries(
                    topic, num_perspectives=self.num_perspectives
                )
            )
        except Exception as exc:  # noqa: BLE001 - stage best-effort, nunca aborta o pipeline
            logger.warning(f"storm_stage.failed: {exc}")
            context.record_error("storm", exc, critical=False)
            return context

        seed_queries = self._flatten_sub_queries(perspectives)

        context.set("storm_perspectives", perspectives)
        context.set("storm_seed_queries", seed_queries)

        logger.info(
            f"StormStage: {len(perspectives)} perspectivas geradas, "
            f"{len(seed_queries)} seed queries únicas."
        )
        return context

    @staticmethod
    def _flatten_sub_queries(perspectives: list[dict[str, Any]]) -> list[str]:
        """Achata `sub_queries` de todas as personas, deduplicando por texto."""
        seed_queries: list[str] = []
        for perspective in perspectives:
            for sub_query in perspective.get("sub_queries", []) or []:
                sub_query = str(sub_query).strip()
                if sub_query and sub_query not in seed_queries:
                    seed_queries.append(sub_query)
        return seed_queries
