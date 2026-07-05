"""
graph_explorer_agent.py — Agente de Travessia e Análise de Densidade do Grafo

Responsabilidade:
  Atravessa o Grafo de Conhecimento (KuzuDB/Neo4j) de forma autônoma para:
  1. Detectar "ilhas de conhecimento" — nós sem conexões suficientes.
  2. Calcular a densidade de conexões entre comunidades temáticas (Leiden/Louvain).
  3. Identificar pontes semânticas fracas que precisam de mais pesquisa.
  4. Gerar "gap queries" direcionadas para preencher os espaços estruturais do grafo.

Pipeline:
  traverse(session_id) → analisa subgrafo → retorna GraphGapReport com queries sugeridas
  O orchestrator pode consumir o GraphGapReport para reabrir o ciclo de busca
  com as queries de gap, enriquecendo o grafo de forma iterativa.

Integração:
  - Chamado pelo Orchestrator após a etapa de Knowledge Graph (Etapa 6).
  - Depende de `src.memory.knowledge_graph.KnowledgeGraph` como fonte de dados.
  - Resultados de gap são passados como ExpandedQuery extras ao QueryExpander.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("graph_explorer_agent")


# ─── Constantes ───────────────────────────────────────────────────────────────

# Número mínimo de conexões para um nó ser considerado "conectado"
MIN_EDGE_COUNT_THRESHOLD = 2
# Score mínimo de similaridade semântica para uma aresta ser considerada "forte"
MIN_EDGE_WEIGHT_THRESHOLD = 0.60
# Número máximo de gap queries geradas por chamada (budget control)
MAX_GAP_QUERIES = 5


# ─── Data Contracts ────────────────────────────────────────────────────────────


@dataclass
class KnowledgeNode:
    """Nó simplificado do Grafo de Conhecimento para análise de densidade."""

    node_id: str
    label: str
    node_type: str  # "concept", "entity", "claim", "source"
    edge_count: int = 0
    community_id: int = -1
    embedding_available: bool = False


@dataclass
class KnowledgeBridge:
    """Ponte semântica fraca entre duas comunidades temáticas."""

    community_a: int
    community_b: int
    bridge_node_id: str
    edge_weight: float  # 0.0-1.0
    gap_query_suggestion: str = ""


@dataclass
class GraphGapReport:
    """Relatório de gaps estruturais detectados no Grafo de Conhecimento."""

    session_id: str
    total_nodes_analyzed: int = 0
    isolated_nodes: list[KnowledgeNode] = field(default_factory=list)
    weak_bridges: list[KnowledgeBridge] = field(default_factory=list)
    gap_queries: list[str] = field(default_factory=list)
    community_count: int = 0
    graph_density: float = 0.0  # 0.0 = grafo vazio, 1.0 = grafo completo

    @property
    def has_gaps(self) -> bool:
        """Indica se há gaps significativos que demandam mais pesquisa."""
        return len(self.isolated_nodes) > 0 or len(self.weak_bridges) > 0

    @property
    def gap_severity(self) -> str:
        """Severidade dos gaps detectados."""
        total_gaps = len(self.isolated_nodes) + len(self.weak_bridges)
        if total_gaps == 0:
            return "none"
        if total_gaps <= 3:
            return "low"
        if total_gaps <= 8:
            return "medium"
        return "high"


# ─── Agente Principal ──────────────────────────────────────────────────────────


class GraphExplorerAgent:
    """Agente autônomo de análise de densidade e gaps no Grafo de Conhecimento do SRA.

    Percorre o subgrafo de uma sessão de pesquisa, detecta nós isolados, pontes
    semânticas fracas e clusters desconexos, e sugere queries de preenchimento.

    Uso básico:
        agent = GraphExplorerAgent(knowledge_graph=orchestrator.knowledge_graph)
        report = await agent.traverse(session_id="abc123", query_topic="machine learning")
        if report.has_gaps:
            for gap_q in report.gap_queries:
                # Adiciona ao pipeline de busca
    """

    def __init__(self, knowledge_graph: Any = None, llm: Any = None) -> None:
        self._kg = knowledge_graph
        self._llm = llm
        logger.info(
            f"GraphExplorerAgent inicializado. "
            f"KnowledgeGraph: {'conectado' if knowledge_graph else 'mock/ausente'}."
        )

    async def traverse(
        self,
        session_id: str,
        query_topic: str = "",
        max_nodes: int = 200,
    ) -> GraphGapReport:
        """Traversa o subgrafo de uma sessão e detecta gaps estruturais.

        Args:
            session_id: ID da sessão de pesquisa para filtrar os nós relevantes.
            query_topic: Tema central da pesquisa (usado para contextualizar gap queries).
            max_nodes: Limite de nós analisados para controle de custo computacional.

        Returns:
            GraphGapReport com isolated_nodes, weak_bridges e gap_queries prontas.
        """
        report = GraphGapReport(session_id=session_id)
        logger.info(
            f"[GraphExplorer] Iniciando traversal do grafo para sessão {session_id}."
        )

        nodes = await self._fetch_nodes(session_id, max_nodes)
        if not nodes:
            logger.info(
                "[GraphExplorer] Grafo vazio ou não disponível. Retornando relatório vazio."
            )
            return report

        report.total_nodes_analyzed = len(nodes)
        report.graph_density = self._calculate_density(nodes)
        report.community_count = len(
            set(n.community_id for n in nodes if n.community_id >= 0)
        )

        # Detectar nós isolados
        report.isolated_nodes = [
            n for n in nodes if n.edge_count < MIN_EDGE_COUNT_THRESHOLD
        ]

        # Detectar pontes fracas entre comunidades
        report.weak_bridges = await self._detect_weak_bridges(nodes)

        # Gerar gap queries baseadas nos gaps encontrados
        report.gap_queries = await self._generate_gap_queries(
            report.isolated_nodes,
            report.weak_bridges,
            query_topic,
        )

        logger.info(
            f"[GraphExplorer] Traversal concluído: {report.total_nodes_analyzed} nós, "
            f"{len(report.isolated_nodes)} isolados, {len(report.weak_bridges)} pontes fracas, "
            f"{len(report.gap_queries)} gap queries geradas. "
            f"Severidade: {report.gap_severity}."
        )
        return report

    async def _fetch_nodes(
        self, session_id: str, max_nodes: int
    ) -> list[KnowledgeNode]:
        """Busca nós do Grafo de Conhecimento para a sessão especificada."""
        if self._kg is None:
            logger.debug(
                "[GraphExplorer] KnowledgeGraph não conectado. Usando stub vazio."
            )
            return []

        try:
            # Tenta chamar a interface real do KnowledgeGraph
            if hasattr(self._kg, "get_session_nodes"):
                raw_nodes = await self._kg.get_session_nodes(
                    session_id, limit=max_nodes
                )
                return [
                    KnowledgeNode(
                        node_id=n.get("id", ""),
                        label=n.get("label", ""),
                        node_type=n.get("type", "concept"),
                        edge_count=n.get("edge_count", 0),
                        community_id=n.get("community_id", -1),
                        embedding_available=n.get("embedding") is not None,
                    )
                    for n in raw_nodes
                ]
            # Fallback: interface genérica do KuzuDB
            if hasattr(self._kg, "query"):
                cypher = (
                    f"MATCH (n) WHERE n.session_id = '{session_id}' "
                    f"RETURN n LIMIT {max_nodes}"
                )
                results = await self._kg.query(cypher)
                return [
                    KnowledgeNode(
                        node_id=str(r.get("n", {}).get("id", i)),
                        label=str(r.get("n", {}).get("label", f"node_{i}")),
                        node_type=r.get("n", {}).get("type", "concept"),
                        edge_count=r.get("n", {}).get("edge_count", 0),
                    )
                    for i, r in enumerate(results)
                ]
        except Exception as e:
            logger.warning(f"[GraphExplorer] Erro ao buscar nós do grafo: {e}")
        return []

    def _calculate_density(self, nodes: list[KnowledgeNode]) -> float:
        """Calcula a densidade do grafo como proporção de nós bem conectados."""
        if not nodes:
            return 0.0
        well_connected = sum(
            1 for n in nodes if n.edge_count >= MIN_EDGE_COUNT_THRESHOLD
        )
        return round(well_connected / len(nodes), 4)

    async def _detect_weak_bridges(
        self, nodes: list[KnowledgeNode]
    ) -> list[KnowledgeBridge]:
        """Detecta nós que fazem pontes fracas entre comunidades distintas.

        Uma ponte fraca é um nó que conecta duas comunidades diferentes mas
        com peso de aresta abaixo do threshold mínimo de similaridade semântica.
        """
        bridges: list[KnowledgeBridge] = []
        if self._kg is None:
            return bridges

        try:
            communities = {}
            for node in nodes:
                if node.community_id >= 0:
                    communities.setdefault(node.community_id, []).append(node)

            community_ids = list(communities.keys())
            for i in range(len(community_ids)):
                for j in range(i + 1, len(community_ids)):
                    com_a = community_ids[i]
                    com_b = community_ids[j]
                    # Busca o nó com mais conexões entre as duas comunidades
                    bridge_candidates = [
                        n
                        for n in nodes
                        if n.community_id in (com_a, com_b) and n.edge_count >= 1
                    ]
                    if not bridge_candidates:
                        continue
                    # Heurística: menor edge_count relativo ao total indica ponte fraca
                    weakest = min(bridge_candidates, key=lambda n: n.edge_count)
                    max_edges = max(n.edge_count for n in nodes) or 1
                    edge_weight = weakest.edge_count / max_edges

                    if edge_weight < MIN_EDGE_WEIGHT_THRESHOLD:
                        suggestion = f"conexão entre {com_a} e {com_b}: {weakest.label}"
                        bridges.append(
                            KnowledgeBridge(
                                community_a=com_a,
                                community_b=com_b,
                                bridge_node_id=weakest.node_id,
                                edge_weight=round(edge_weight, 4),
                                gap_query_suggestion=suggestion,
                            )
                        )
        except Exception as e:
            logger.warning(f"[GraphExplorer] Erro ao detectar pontes fracas: {e}")
        return bridges

    async def _generate_gap_queries(
        self,
        isolated: list[KnowledgeNode],
        bridges: list[KnowledgeBridge],
        topic: str,
    ) -> list[str]:
        """Gera queries direcionadas para preencher os gaps do grafo.

        Se um LLM estiver disponível, usa geração contextual. Caso contrário,
        usa templates de gap queries baseados nos rótulos dos nós isolados.
        """
        queries: list[str] = []

        # Queries para nós isolados
        for node in isolated[:3]:  # Limita a 3 nós isolados
            if self._llm:
                try:
                    prompt = (
                        f"O nó '{node.label}' está isolado no grafo de conhecimento "
                        f"sobre o tema '{topic}'. Gere 1 query de busca objetiva "
                        "para encontrar fontes que conectem esse conceito ao contexto geral. "
                        "Responda APENAS com a query, sem explicações."
                    )
                    response = await self._llm.complete(prompt, max_tokens=60)
                    query = response.strip().strip('"').strip("'")
                    if query:
                        queries.append(query)
                        continue
                except Exception as e:
                    logger.debug(f"[GraphExplorer] Falha no LLM para gap query: {e}")
            # Fallback sem LLM
            queries.append(f"{node.label} {topic} relacionamento evidências")

        # Queries para pontes fracas
        for bridge in bridges[:2]:  # Limita a 2 pontes
            queries.append(bridge.gap_query_suggestion)

        return queries[:MAX_GAP_QUERIES]

    async def get_community_summary(self) -> dict[int, dict[str, Any]]:
        """Retorna um resumo das comunidades do grafo com contagem de nós e rótulos centrais.

        Útil para visualização e debugging da estrutura temática do grafo.

        Returns:
            Dicionário com community_id como chave e metadados como valor.
        """
        if self._kg is None:
            return {}
        try:
            if hasattr(self._kg, "get_communities"):
                return await self._kg.get_communities()
        except Exception as e:
            logger.warning(f"[GraphExplorer] Erro ao buscar resumo de comunidades: {e}")
        return {}
