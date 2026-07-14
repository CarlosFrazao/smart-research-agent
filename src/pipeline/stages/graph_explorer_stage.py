"""src/pipeline/stages/graph_explorer_stage.py — Stage do GraphExplorerAgent.

Expõe o `GraphExplorerAgent` (src/graph_explorer_agent.py) como uma etapa
real do `ResearchPipeline`, executada logo após o `ScoreStage` e antes do
`SynthesizeStage`.

Contexto / motivação
---------------------
Antes desta stage, `GraphExplorerAgent` existia no repositório mas
`grep -rn "GraphExplorerAgent"` retornava zero referências fora do próprio
arquivo — não estava registrado em `StageFactory` nem conectado ao
`ResearchPipeline`. O slot "gap" do pipeline (`GapFillStage`, em
`src/pipeline/stages/__init__.py`) também segue sendo, até hoje, um stub
vazio (`async def run(...): pass`); nada no repositório popula ou lê
`PipelineContext.gap_analysis`. Esta stage assume esse papel: roda a
análise de densidade do Grafo de Conhecimento e usa o mesmo campo tipado
já existente (`gap_analysis`) em vez de inventar um novo.

Limitações honestas (leia antes de estender)
---------------------------------------------
1. **Sem loop de re-busca automático.** O `ResearchPipeline`
   (`src/pipeline/pipeline.py`) executa cada stage exatamente uma vez, em
   sequência linear — não existe hoje uma segunda rodada de `SearchStage`
   que consuma novas entradas de `context.expanded_queries`. As
   `ExpandedQuery` produzidas por `GraphGapReport.to_expanded_queries()`
   são anexadas a `context.expanded_queries` (contrato pedido: "consumir
   report.to_expanded_queries() e injetar de volta no QueryExpander") e
   também preservadas em `context.gap_analysis`, mas isso NÃO dispara uma
   nova busca nesta mesma execução — hoje serve como trilha de auditoria e
   como ponto de extensão pronto para quando/se o pipeline ganhar um loop
   iterativo real. Não afirmamos uma integração mais profunda do que o
   pipeline atual permite.
2. **Backend real do grafo.** Usa `orchestrator.memory.kg`
   (`OrvixMemoryV2.kg` — instância real de
   `src.knowledge_graph.SemanticKnowledgeGraph`, já criada dentro de
   `OrvixMemory.__init__`, ver `src/memory/orvix_memory.py:137`) e NÃO
   `orchestrator.knowledge_graph` (isso é
   `src.memory.knowledge_graph.KnowledgeGraph`, wrapper fino de Neo4j sem
   `query_graph()`/comunidades — ver docstring de `graph_explorer_agent.py`
   para a distinção completa). Reaproveitamos a conexão KuzuDB já aberta
   por `OrvixMemory` em vez de abrir uma segunda (diferente de uma versão
   anterior deste plano, que sugeria instanciar um novo
   `SemanticKnowledgeGraph(kuzu_conn=memory.kuzu_conn)` na factory — sem
   necessidade, já existe um pronto em `memory.kg`).
   Se `orchestrator.memory` for `None` (memória desabilitada ou falhou ao
   inicializar) ou não expuser `.kg`, o stage loga o motivo e segue com
   `knowledge_graph=None` — o próprio `GraphExplorerAgent` já sabe devolver
   um relatório vazio nesse caso em vez de falhar.
3. **`session_id` é apenas para logging/contexto**, não filtra dados (ver
   docstring de `graph_explorer_agent.py` — schema de `add_triple`/
   `add_fact` precisaria de uma coluna `session_id` para isolamento real;
   isso é o "Passo 3 (opcional)" do plano original e não foi feito aqui).
4. **Best-effort.** `critical = False`: uma falha aqui nunca aborta o
   pipeline nem dispara rollback — mesma política usada para
   EvidenceGraph/PeerReview/ConflictDetector no `Orchestrator` legado.
"""

from __future__ import annotations

import logging
from typing import Any

from src.graph_explorer_agent import GraphExplorerAgent, GraphGapReport
from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("pipeline.graph_explorer_stage")


class GraphExplorerStage(PipelineStage):
    """Stage best-effort que analisa o Grafo de Conhecimento e sugere gap queries.

    Parameters
    ----------
    graph_explorer_agent:
        Instância pré-construída de `GraphExplorerAgent`. Se `None`, o
        stage resolve (e cacheia) uma instância na primeira execução,
        usando `context.extras["orchestrator"]` para localizar
        `orchestrator.memory.kg` e `orchestrator.llm` (ver limitações no
        docstring do módulo).
    """

    name = "graph_explorer"
    critical = False  # análise exploratória: nunca deve abortar o pipeline

    def __init__(self, graph_explorer_agent: GraphExplorerAgent | None = None) -> None:
        self._agent = graph_explorer_agent

    async def run(self, context: PipelineContext) -> PipelineContext:
        agent = self._resolve_agent(context)
        session_id = context.extras.get("session_id", "default_session")

        try:
            report: GraphGapReport = await agent.traverse(
                session_id=session_id, query_topic=context.query
            )
        except Exception as e:
            logger.warning(f"[GraphExplorerStage] traverse() falhou (ignorado): {e}")
            return context

        # Reaproveita o campo tipado já existente em PipelineContext — ver
        # motivação no docstring do módulo (ele existe, mas hoje nada o
        # preenche).
        context.gap_analysis = report

        if not report.has_gaps:
            logger.info(
                f"[GraphExplorerStage] {report.total_nodes_analyzed} nós analisados, "
                "nenhum gap estrutural detectado."
            )
            return context

        new_queries = report.to_expanded_queries()
        existing_texts = {getattr(q, "query", str(q)) for q in context.expanded_queries}
        appended = 0
        for eq in new_queries:
            if eq.query not in existing_texts:
                context.expanded_queries.append(eq)
                existing_texts.add(eq.query)
                appended += 1

        logger.info(
            f"[GraphExplorerStage] severidade={report.gap_severity}: "
            f"{len(report.isolated_nodes)} nós isolados, "
            f"{len(report.weak_bridges)} pontes fracas, "
            f"{appended} gap queries anexadas a expanded_queries "
            "(sem re-busca automática nesta execução — pipeline é linear/"
            "single-pass; ver limitação 1 no docstring do módulo)."
        )
        return context

    def _resolve_agent(self, context: PipelineContext) -> GraphExplorerAgent:
        if self._agent is not None:
            return self._agent

        orchestrator = context.extras.get("orchestrator")
        kg: Any = None
        if orchestrator is not None:
            memory = getattr(orchestrator, "memory", None)
            kg = getattr(memory, "kg", None) if memory is not None else None
            if kg is None:
                logger.info(
                    "[GraphExplorerStage] orchestrator.memory.kg indisponível "
                    "(memória desabilitada, falhou ao inicializar, ou backend "
                    "sem SemanticKnowledgeGraph); GraphExplorerAgent rodará "
                    "sem grafo (retorna relatório vazio, não falha)."
                )

        llm = getattr(orchestrator, "llm", None) if orchestrator is not None else None
        self._agent = GraphExplorerAgent(knowledge_graph=kg, llm=llm)
        return self._agent


__all__ = ["GraphExplorerStage"]
