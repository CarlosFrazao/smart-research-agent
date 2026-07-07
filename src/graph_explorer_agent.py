"""
graph_explorer_agent.py — Agente de Travessia e Análise de Densidade do Grafo

Responsabilidade:
  Atravessa o Grafo de Conhecimento semântico do SRA (`src.knowledge_graph.
  SemanticKnowledgeGraph`, backend KuzuDB) de forma autônoma para:
  1. Detectar "ilhas de conhecimento" — entidades sem conexões suficientes.
  2. Calcular a densidade de conexões entre comunidades temáticas (Louvain).
  3. Identificar pontes semânticas fracas (baixa confiança) entre comunidades.
  4. Gerar "gap queries" direcionadas para preencher os espaços estruturais do grafo.

Pipeline:
  traverse(session_id) → analisa o grafo → retorna GraphGapReport com queries sugeridas
  `GraphGapReport.to_expanded_queries()` converte o resultado para `src.types.
  ExpandedQuery` (type="graph_gap"), o mesmo contrato usado por `GapDetector`
  (type="gap_fill"), `ConflictDetector` (type="fact_check") e
  `DebateOrchestrator` (type="debate_hypothesis") — permitindo que o
  orchestrator injete essas queries de volta no `QueryExpander`/loop de busca
  sem conversão manual.

Integração e limitações conhecidas (leia antes de usar):
  - Este agente espera um objeto com a interface de
    `src.knowledge_graph.SemanticKnowledgeGraph` (método `query_graph()` →
    `list[Triple]`, e opcionalmente `detect_communities()`). Essa é a única
    implementação do repositório com detecção de comunidade real (Louvain via
    networkx); ela hoje só é instanciada dentro de `OrvixMemory`, que por sua
    vez só é usada em testes — NÃO é o `orchestrator.knowledge_graph` padrão
    (esse é `src.memory.knowledge_graph.KnowledgeGraph`, um wrapper fino de
    Neo4j sem `query_graph`/listagem de nós/comunidades). Se `knowledge_graph`
    for passado sem essa interface, o agente loga o motivo e devolve um
    relatório vazio em vez de falhar silenciosamente.
  - O grafo atual (em qualquer backend) NÃO armazena `session_id` por nó/
    relação. Não existe hoje um jeito real de isolar "o subgrafo desta
    sessão" — `session_id` é usado apenas para logging e para contextualizar
    as gap queries geradas, não como filtro de dados. Se precisar de
    isolamento real por sessão, o schema de `add_triple`/`add_fact` precisa
    ganhar essa coluna primeiro.
  - Antes desta correção, o agente chamava métodos que não existem em nenhum
    backend real do projeto (`get_session_nodes`, `.query()` com Cypher cru,
    `get_communities`) e por isso sempre retornava um relatório vazio sem
    nunca lançar erro — e não estava conectado ao Orchestrator em lugar
    nenhum do pipeline (`grep` confirma zero referências fora deste arquivo).
    Este arquivo corrige a busca de dados para usar a interface real; a
    integração no pipeline (registrar como stage em
    `src/pipeline/stage_factory.py`) ainda precisa ser feita à parte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("graph_explorer_agent")


# ─── Constantes ───────────────────────────────────────────────────────────────

# Número mínimo de conexões para uma entidade ser considerada "conectada"
MIN_EDGE_COUNT_THRESHOLD = 2
# Confiança mínima de aresta para uma ponte entre comunidades ser "forte"
MIN_EDGE_WEIGHT_THRESHOLD = 0.60
# Número máximo de gap queries geradas por chamada (budget control)
MAX_GAP_QUERIES = 5
# task_type usado no LLMClient.complete() para roteamento/orçamento corretos
LLM_TASK_TYPE = "gap_query"


# ─── Data Contracts ────────────────────────────────────────────────────────────


@dataclass
class KnowledgeNode:
    """Nó simplificado do Grafo de Conhecimento para análise de densidade."""

    node_id: str
    label: str
    node_type: str = "entity"  # "concept", "entity", "claim", "source"
    edge_count: int = 0
    community_id: int = -1
    embedding_available: bool = False


@dataclass
class KnowledgeBridge:
    """Ponte semântica fraca entre duas comunidades temáticas."""

    community_a: int
    community_b: int
    bridge_node_id: str
    edge_weight: float  # 0.0-1.0 — confiança real da aresta (Triple.confidence)
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

    def to_expanded_queries(
        self, priority: Literal["alta", "media", "baixa"] = "media"
    ) -> list[Any]:
        """Converte `gap_queries` para `src.types.ExpandedQuery`.

        Usa o mesmo contrato (`query`, `type`, `priority`, `rationale`) já
        consumido por `GapDetector`/`ConflictDetector`/`DebateOrchestrator`,
        para que o orchestrator possa injetar essas queries de volta no
        `QueryExpander`/loop de busca sem conversão manual.

        Import de `ExpandedQuery` é feito de forma tardia (lazy) para não
        acoplar este módulo a `src.types` quando usado de forma standalone
        (ex.: testes unitários sem o resto do pacote `src` disponível).
        """
        try:
            from src.types import ExpandedQuery
        except ImportError:
            logger.warning(
                "[GraphExplorer] src.types.ExpandedQuery indisponível; "
                "retornando lista vazia em to_expanded_queries()."
            )
            return []

        return [
            ExpandedQuery(
                query=q,
                type="graph_gap",
                priority=priority,
                rationale=(
                    f"Gap estrutural detectado no grafo de conhecimento "
                    f"(sessão {self.session_id}, severidade {self.gap_severity})."
                ),
            )
            for q in self.gap_queries
        ]


# ─── Agente Principal ──────────────────────────────────────────────────────────


class GraphExplorerAgent:
    """Agente autônomo de análise de densidade e gaps no Grafo de Conhecimento do SRA.

    Percorre o Grafo de Conhecimento, detecta entidades isoladas, pontes
    semânticas fracas entre comunidades (via Louvain) e sugere queries de
    preenchimento.

    Backend esperado: `src.knowledge_graph.SemanticKnowledgeGraph` (ou
    qualquer objeto duck-typed com `query_graph()` retornando `list[Triple]`
    e, opcionalmente, `detect_communities()`). Veja as limitações de
    `session_id` e de backend no docstring do módulo.

    Uso básico:
        from src.knowledge_graph import SemanticKnowledgeGraph
        kg = SemanticKnowledgeGraph(kuzu_conn=orchestrator_memory.kuzu_conn)
        agent = GraphExplorerAgent(knowledge_graph=kg, llm=orchestrator.llm)
        report = await agent.traverse(session_id="abc123", query_topic="machine learning")
        if report.has_gaps:
            novas_queries = report.to_expanded_queries()  # -> list[ExpandedQuery]
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
        """Analisa o Grafo de Conhecimento e detecta gaps estruturais.

        Args:
            session_id: ID da sessão de pesquisa. Usado apenas para logging e
                para contextualizar as gap queries — o grafo atual não
                armazena session_id por nó/relação, então isto NÃO filtra os
                dados analisados (ver limitações no docstring do módulo).
            query_topic: Tema central da pesquisa (usado para contextualizar gap queries).
            max_nodes: Limite de nós analisados para controle de custo computacional.

        Returns:
            GraphGapReport com isolated_nodes, weak_bridges e gap_queries prontas.
        """
        report = GraphGapReport(session_id=session_id)
        logger.info(
            f"[GraphExplorer] Iniciando análise do grafo (contexto sessão={session_id})."
        )

        nodes = await self._fetch_nodes(max_nodes)
        if not nodes:
            logger.info(
                "[GraphExplorer] Grafo vazio, indisponível, ou backend sem "
                "interface suportada. Retornando relatório vazio."
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
            f"[GraphExplorer] Análise concluída: {report.total_nodes_analyzed} nós, "
            f"{len(report.isolated_nodes)} isolados, {len(report.weak_bridges)} pontes fracas, "
            f"{len(report.gap_queries)} gap queries geradas. "
            f"Severidade: {report.gap_severity}."
        )
        return report

    async def _fetch_nodes(self, max_nodes: int) -> list[KnowledgeNode]:
        """Busca nós reais do Grafo de Conhecimento e computa grau/comunidade.

        Ordem de tentativa:
          1. `get_session_nodes` — mantido para compatibilidade com mocks/
             testes e para um futuro backend com scoping real de sessão.
          2. `query_graph()` — interface real de `SemanticKnowledgeGraph`.
             Constrói grau (edge_count) e comunidade (Louvain) a partir das
             triplas retornadas, já que o backend não expõe isso pronto por
             nó.
        Não há mais fallback de Cypher cru (`.query()` com f-string): além de
        não existir em nenhum backend real do projeto, montava a query por
        interpolação de string — risco de injeção caso algum backend viesse
        a expor esse método no futuro.
        """
        if self._kg is None:
            logger.debug(
                "[GraphExplorer] KnowledgeGraph não conectado. Usando stub vazio."
            )
            return []

        try:
            if hasattr(self._kg, "get_session_nodes"):
                raw_nodes = await self._kg.get_session_nodes(limit=max_nodes)
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

            if hasattr(self._kg, "query_graph"):
                return self._build_nodes_from_triples(max_nodes)

            logger.warning(
                "[GraphExplorer] Backend de KnowledgeGraph não expõe "
                "get_session_nodes() nem query_graph(); nenhuma entidade "
                "pode ser listada (ex.: KnowledgeGraph do Neo4j em "
                "src/memory/knowledge_graph.py só suporta add_fact/query_entity)."
            )
        except Exception as e:
            logger.warning(f"[GraphExplorer] Erro ao buscar nós do grafo: {e}")
        return []

    def _build_nodes_from_triples(self, max_nodes: int) -> list[KnowledgeNode]:
        """Constrói `KnowledgeNode`s a partir de `SemanticKnowledgeGraph.query_graph()`.

        Calcula edge_count real (grau) contando ocorrências de cada entidade
        como sujeito/objeto, e community_id real via Louvain (mesma técnica
        usada em `SemanticKnowledgeGraph.detect_communities()`), sem
        depender de nenhum dado fabricado.
        """
        triples = self._kg.query_graph()
        if not triples:
            return []

        degree: dict[str, int] = {}
        for t in triples:
            degree[t.subject] = degree.get(t.subject, 0) + 1
            degree[t.object] = degree.get(t.object, 0) + 1

        community_of: dict[str, int] = {}
        try:
            import networkx as nx
            from networkx.algorithms.community import louvain_communities

            graph = nx.Graph()
            for t in triples:
                graph.add_edge(t.subject, t.object)
            communities = louvain_communities(graph)
            for idx, community in enumerate(communities):
                for entity in community:
                    community_of[entity] = idx
        except ImportError:
            logger.debug(
                "[GraphExplorer] networkx indisponível; community_id "
                "permanecerá -1 para todas as entidades."
            )
        except Exception as e:
            logger.warning(f"[GraphExplorer] Falha na detecção de comunidades: {e}")

        nodes = [
            KnowledgeNode(
                node_id=entity,
                label=entity,
                node_type="entity",
                edge_count=count,
                community_id=community_of.get(entity, -1),
            )
            for entity, count in degree.items()
        ]
        return nodes[:max_nodes]

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
        """Detecta entidades que fazem pontes fracas entre comunidades distintas.

        Uma ponte fraca é uma aresta que conecta duas comunidades diferentes
        com confiança (`Triple.confidence`) abaixo de MIN_EDGE_WEIGHT_THRESHOLD.
        Quando o backend expõe `query_graph()`, usa a confiança real das
        triplas em vez de estimar peso a partir do grau (heurística antiga,
        que confundia "conexão fraca" com "nó pouco conectado").
        """
        bridges: list[KnowledgeBridge] = []
        if self._kg is None:
            return bridges

        community_of = {n.node_id: n.community_id for n in nodes if n.community_id >= 0}
        if not community_of:
            return bridges

        try:
            if hasattr(self._kg, "query_graph"):
                triples = self._kg.query_graph()
                seen_pairs: set[tuple[int, int]] = set()
                for t in triples:
                    com_a = community_of.get(t.subject)
                    com_b = community_of.get(t.object)
                    if com_a is None or com_b is None or com_a == com_b:
                        continue
                    pair = (min(com_a, com_b), max(com_a, com_b))
                    if pair in seen_pairs:
                        continue
                    confidence = float(getattr(t, "confidence", 1.0))
                    if confidence < MIN_EDGE_WEIGHT_THRESHOLD:
                        seen_pairs.add(pair)
                        suggestion = (
                            f"conexão entre comunidades {pair[0]} e {pair[1]}: "
                            f"{t.subject} -[{t.relation}]-> {t.object}"
                        )
                        bridges.append(
                            KnowledgeBridge(
                                community_a=pair[0],
                                community_b=pair[1],
                                bridge_node_id=t.subject,
                                edge_weight=round(confidence, 4),
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
                    response = await self._llm.complete(
                        prompt, task_type=LLM_TASK_TYPE, max_tokens=60
                    )
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
            if hasattr(self._kg, "detect_communities"):
                communities = self._kg.detect_communities()
                return {
                    idx: {"size": len(members), "members": list(members)[:10]}
                    for idx, members in enumerate(communities)
                }
        except Exception as e:
            logger.warning(f"[GraphExplorer] Erro ao buscar resumo de comunidades: {e}")
        return {}
