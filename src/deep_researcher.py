"""
DeepResearcher — Tree-based Deep Research Engine

Philosophy: non-linear reasoning — the agent can branch into parallel
sub-queries, prune dead ends based on evidence, and consolidate only
confirmed hypotheses into the final report.

Usage: activated only when --mode deep is passed. Cost ~5-10x standard.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import uuid
from dataclasses import dataclass, field

from src.clients.llm_client import LLMClient
from src.exceptions import BudgetExceededError
from src.token_economy import TokenEconomy
from src.types import SearchResult, ExpandedQuery, IntentResult, Domain, Intention

logger = logging.getLogger(__name__)


@dataclass
class ResearchBudget:
    """Orçamento configurável para uma pesquisa profunda."""

    max_total_nodes: int = 20
    max_depth: int = 3
    max_branches_per_node: int = 3
    max_llm_calls: int = 50
    max_tokens_total: int = 100_000
    max_cost_usd: float = 5.0

    # Contadores internos (não configurar externamente):
    nodes_created: int = field(default=0, repr=False)
    llm_calls: int = field(default=0, repr=False)
    tokens_used: int = field(default=0, repr=False)
    estimated_cost: float = field(default=0.0, repr=False)

    def is_exhausted(self) -> bool:
        return (
            self.nodes_created >= self.max_total_nodes
            or self.llm_calls >= self.max_llm_calls
            or self.tokens_used >= self.max_tokens_total
            or self.estimated_cost >= self.max_cost_usd
        )

    def summary(self) -> dict:
        return {
            "nodes": f"{self.nodes_created}/{self.max_total_nodes}",
            "llm_calls": f"{self.llm_calls}/{self.max_llm_calls}",
            "tokens": f"{self.tokens_used}/{self.max_tokens_total}",
            "cost_usd": f"${self.estimated_cost:.4f}/${self.max_cost_usd}",
        }


@dataclass
class ResearchNode:
    """One node in the reasoning tree."""

    id: str
    query: str
    hypothesis: str
    results: list[SearchResult] = field(default_factory=list)
    children: list["ResearchNode"] = field(default_factory=list)
    status: str = "pending"  # pending | explored | dead_end | confirmed | pruned_low_confidence | pruned_beam | pruned_budget
    confidence: float = 0.0
    depth: int = 0
    reasoning: str = ""
    angle: str = "general"  # confirmative | contrasting | alternative | general


@dataclass
class DeepResearchResult:
    """Output of a DeepResearcher.research() call."""

    findings: list[SearchResult]
    reasoning_tree: str  # markdown representation of the tree
    total_nodes_explored: int
    confirmed_hypotheses: list[str]
    dead_end_hypotheses: list[str]
    budget_exceeded: bool = False
    budget_summary: dict | None = None


class DeepResearcher:
    """
    Orchestrates multi-depth research with a reasoning tree.

    Flow:
    1. Receives the original query
    2. Generates competing hypotheses (branches)
    3. Searches for each hypothesis in parallel
    4. Analyses results: confirms, refutes, or generates sub-hypotheses
    5. Prunes branches that become dead ends
    6. Consolidates confirmed branches into final findings
    7. Exports the reasoning tree as readable markdown
    """

    MAX_DEPTH: int = 3
    MAX_BRANCHES: int = 4
    BEAM_WIDTH: int = 1  # top-K mantido por nível (padrão 1 para limite de 13 nós)
    MIN_CONFIDENCE: float = 0.55  # abaixo disso, o nó é podado (early pruning)
    CONFIRMED_THRESHOLD: float = 0.75

    MODEL_PRICES = {
        "gemma-4-26b": 0.0,  # Free no OpenRouter
        "gemini-2.5-flash": 0.0001,  # Muito barato
        "gpt-4o": 0.005,
        "claude-3-5-sonnet": 0.003,
        "default": 0.001,
    }

    def __init__(
        self,
        llm_client: LLMClient,
        orchestrator=None,
        memory=None,
        budget: ResearchBudget | None = None,
    ):
        self.llm = llm_client
        self.orchestrator = orchestrator
        # OrvixMemoryV2 opcional — injeta contexto do grafo nas hipóteses
        self.memory = memory
        self.config = getattr(orchestrator, "config", None)

        # Sincroniza parâmetros baseados nas constantes/configuração
        if self.config:
            self.MAX_DEPTH = getattr(self.config, "max_research_depth", 3)
            self.MAX_BRANCHES = getattr(self.config, "max_research_branches", 4)
            self.BEAM_WIDTH = getattr(self.config, "beam_width", 1)
            self.MIN_CONFIDENCE = getattr(self.config, "min_confidence", 0.55)

        if budget is not None:
            self.budget = budget
        else:
            max_nodes = 20
            max_cost = 5.0
            if self.config:
                max_nodes = getattr(self.config, "max_research_nodes", 20)
                max_cost = getattr(self.config, "max_research_budget_usd", 5.0)
            self.budget = ResearchBudget(
                max_total_nodes=max_nodes, max_cost_usd=max_cost
            )

    async def research(
        self,
        query: str,
        max_iterations: int = 5,
    ) -> DeepResearchResult:
        """
        Executes deep research with tree-based reasoning using Beam Search.
        Returns a DeepResearchResult with reasoning_tree and consolidated findings.
        """
        logger.info(f"DeepResearcher: starting for query='{query[:60]}'")

        root = ResearchNode(
            id="root",
            query=query,
            hypothesis=f"Main research goal: {query}",
            depth=0,
        )

        budget_exceeded = False
        try:
            # 1. Busca e avaliação da raiz
            self.budget.nodes_created += 1
            root.results = await self._search_for_node(root)
            root.confidence = self._estimate_confidence(root.results)
            root.status = "explored"

            # 2. Executa a expansão em Beam Search por nível (BFS)
            frontier = [root]

            for depth in range(self.MAX_DEPTH):
                if not frontier or self.budget.is_exhausted():
                    break

                next_frontier = []
                for node in frontier:
                    if node.status in ("confirmed", "dead_end"):
                        continue

                    # Poda precoce (Early Pruning)
                    if node.depth > 0 and node.confidence < self.MIN_CONFIDENCE:
                        node.status = "pruned_low_confidence"
                        node.reasoning = f"Confidence {node.confidence:.2f} below threshold {self.MIN_CONFIDENCE}. Pruned."
                        continue

                    # Gera as hipóteses (branches)
                    hypotheses = await self._generate_hypotheses(
                        node.query, node.results
                    )
                    for raw_hyp in hypotheses[: self.MAX_BRANCHES]:
                        await self._check_budget()

                        # Extrai o prefixo/ângulo se existir
                        angle = "general"
                        hyp = raw_hyp
                        if ":" in raw_hyp:
                            parts = raw_hyp.split(":", 1)
                            possible_angle = parts[0].strip().lower()
                            if possible_angle in (
                                "confirmative",
                                "contrasting",
                                "alternative",
                            ):
                                angle = possible_angle
                                hyp = parts[1].strip()

                        child = ResearchNode(
                            id=str(uuid.uuid4())[:8],
                            query=hyp,
                            hypothesis=hyp,
                            depth=depth + 1,
                            angle=angle,
                        )
                        node.children.append(child)
                        next_frontier.append(child)

                if not next_frontier:
                    break

                # Executa buscas e estimativa de confiança do nível em paralelo
                search_tasks = [
                    self._explore_child_data(child) for child in next_frontier
                ]
                if search_tasks:
                    await asyncio.gather(*search_tasks)

                # Aplica o Beam Search: mantém apenas os top `BEAM_WIDTH` de maior confiança
                if len(next_frontier) > self.BEAM_WIDTH:
                    survivors = heapq.nlargest(
                        self.BEAM_WIDTH, next_frontier, key=lambda n: n.confidence
                    )
                    survivor_ids = {s.id for s in survivors}
                    for n in next_frontier:
                        if n.id not in survivor_ids:
                            n.status = "pruned_beam"
                            n.reasoning = (
                                f"Pruned by Beam Search (width={self.BEAM_WIDTH})."
                            )
                    frontier = survivors
                else:
                    frontier = next_frontier

            # Ajusta os status dos nós pais com base nos status dos filhos
            self._update_parent_statuses(root)

        except BudgetExceededError as e:
            logger.warning(f"DeepResearcher: budget exceeded: {e}")
            budget_exceeded = True
        except Exception as e:
            logger.error(f"DeepResearcher: erro inesperado no pipeline: {e}")
            raise e

        findings = self._consolidate_tree(root)
        reasoning_tree_md = self._export_tree_as_markdown(root)

        confirmed = self._collect_by_status(root, "confirmed")
        dead_ends = self._collect_by_status(root, "dead_end")
        all_nodes = self._count_nodes(root)

        logger.info(
            f"DeepResearcher: done. nodes={all_nodes}, "
            f"confirmed={len(confirmed)}, dead_ends={len(dead_ends)}, "
            f"findings={len(findings)}"
        )

        return DeepResearchResult(
            findings=findings,
            reasoning_tree=reasoning_tree_md,
            total_nodes_explored=all_nodes,
            confirmed_hypotheses=confirmed,
            dead_end_hypotheses=dead_ends,
            budget_exceeded=budget_exceeded,
            budget_summary=self.budget.summary(),
        )

    async def _check_budget(self) -> None:
        """Lança BudgetExceededError se o orçamento estiver esgotado."""
        if self.budget.is_exhausted():
            raise BudgetExceededError(f"Budget esgotado: {self.budget.summary()}")

    async def _track_llm_call(self, prompt: str) -> None:
        """Incrementa contadores após chamada de LLM."""
        model = "default"
        if self.config:
            model = getattr(self.config, "model", "default")

        if hasattr(self.llm, "token_economy") and isinstance(
            self.llm.token_economy, TokenEconomy
        ):
            tokens = self.llm.token_economy.count_tokens(prompt, model=model)
            _, cost = self.llm.token_economy.estimate_cost(
                prompt, model=model, output_tokens=500
            )
        else:
            tokens = len(prompt) // 4 + 500
            price = self.MODEL_PRICES.get(model, self.MODEL_PRICES["default"])
            cost = (tokens / 1000) * price

        self.budget.llm_calls += 1
        self.budget.tokens_used += tokens
        self.budget.estimated_cost += cost

    async def _explore_child_data(self, child: ResearchNode) -> None:
        """Faz a busca, estima a confiança e define o status de um nó filho individual."""
        try:
            await self._check_budget()
            self.budget.nodes_created += 1

            logger.debug(
                f"Exploring node id={child.id} depth={child.depth} q='{child.query[:50]}'"
            )

            child.results = await self._search_for_node(child)
            child.confidence = self._estimate_confidence(child.results)

            if child.confidence >= self.CONFIRMED_THRESHOLD:
                child.status = "confirmed"
                child.reasoning = f"Confirmed with confidence {child.confidence:.2f} after {len(child.results)} results."
            elif child.depth >= self.MAX_DEPTH:
                child.status = "explored"
                child.reasoning = (
                    f"Max depth reached. Confidence: {child.confidence:.2f}."
                )
            elif child.confidence < self.MIN_CONFIDENCE and child.depth > 0:
                child.status = "dead_end"
                child.reasoning = f"Confidence {child.confidence:.2f} below threshold {self.MIN_CONFIDENCE}. Pruned."
            else:
                child.status = "explored"
        except BudgetExceededError as e:
            child.status = "pruned_budget"
            child.reasoning = f"Budget exceeded: {e}"
        except Exception as e:
            logger.error(f"Erro ao explorar nó {child.id}: {e}")
            child.status = "dead_end"
            child.reasoning = f"Failure: {e}"

    def _update_parent_statuses(self, node: ResearchNode) -> None:
        """Atualiza recursivamente o status dos nós pais com base nos status dos filhos."""
        if not node.children:
            return

        for child in node.children:
            self._update_parent_statuses(child)

        # Filtra filhos válidos (ignora os podados por budget ou beam)
        valid_children = [
            c for c in node.children if c.status not in ("pruned_budget", "pruned_beam")
        ]
        if not valid_children:
            return

        all_dead = all(c.status == "dead_end" for c in valid_children)
        any_confirmed = any(c.status == "confirmed" for c in valid_children)

        if any_confirmed:
            node.status = "confirmed"
            node.reasoning = "Confirmed via child hypotheses."
        elif all_dead:
            node.status = "dead_end"
            node.reasoning = "All child branches are dead ends."
        else:
            node.status = "explored"

    async def _explore_node(self, node: ResearchNode) -> ResearchNode:
        """Mantido estritamente para compatibilidade com os testes unitários legados."""
        await self._check_budget()
        self.budget.nodes_created += 1

        logger.debug(
            f"Exploring node id={node.id} depth={node.depth} q='{node.query[:50]}'"
        )

        node.results = await self._search_for_node(node)
        node.confidence = self._estimate_confidence(node.results)

        if node.confidence >= self.CONFIRMED_THRESHOLD:
            node.status = "confirmed"
            node.reasoning = f"Confirmed with confidence {node.confidence:.2f} after {len(node.results)} results."
            return node

        if node.depth >= self.MAX_DEPTH:
            node.status = "explored"
            node.reasoning = f"Max depth reached. Confidence: {node.confidence:.2f}."
            return node

        if node.confidence < self.MIN_CONFIDENCE and node.depth > 0:
            node.status = "dead_end"
            node.reasoning = f"Confidence {node.confidence:.2f} below threshold {self.MIN_CONFIDENCE}. Pruned."
            return node

        hypotheses = await self._generate_hypotheses(node.query, node.results)

        child_tasks = []
        for raw_hyp in hypotheses[: self.MAX_BRANCHES]:
            angle = "general"
            hyp = raw_hyp
            if ":" in raw_hyp:
                parts = raw_hyp.split(":", 1)
                possible_angle = parts[0].strip().lower()
                if possible_angle in ("confirmative", "contrasting", "alternative"):
                    angle = possible_angle
                    hyp = parts[1].strip()

            child = ResearchNode(
                id=str(uuid.uuid4())[:8],
                query=hyp,
                hypothesis=hyp,
                depth=node.depth + 1,
                angle=angle,
            )
            child_tasks.append(self._explore_node(child))

        if child_tasks:
            node.children = list(await asyncio.gather(*child_tasks))

        all_dead = all(c.status == "dead_end" for c in node.children)
        any_confirmed = any(c.status == "confirmed" for c in node.children)

        if any_confirmed:
            node.status = "confirmed"
            node.reasoning = "Confirmed via child hypotheses."
        elif all_dead:
            node.status = "dead_end"
            node.reasoning = "All child branches are dead ends."
        else:
            node.status = "explored"
            node.reasoning = f"Explored with {len(node.children)} branches."

        return node

    async def _search_for_node(self, node: ResearchNode) -> list[SearchResult]:
        """Searches using the orchestrator if available, otherwise returns empty list."""
        if self.orchestrator is None:
            logger.debug(
                f"No orchestrator attached; skipping search for node {node.id}"
            )
            return []

        try:
            expanded_queries = [
                ExpandedQuery(
                    query=node.query,
                    type="deep_research",
                    priority="alta",
                    rationale=f"deep research node depth={node.depth}",
                )
            ]
            intent = IntentResult(
                domain=Domain.GENERAL,
                intention=Intention.DISCOVER,
                urgency="nao",
                confidence="alta",
            )
            source_plan = self.orchestrator.source_planner.plan(
                intent, expanded_queries
            )
            results = await self.orchestrator._parallel_search(
                expanded_queries, source_plan, intent
            )
            ranked = await self.orchestrator.ranker.rank(results)
            scored = await self.orchestrator.confidence_scorer.score_batch(
                ranked, cross_validate=False
            )
            return scored[:10]
        except Exception as e:
            logger.warning(f"Search for node {node.id} failed: {e}")
            # Arquiva a falha na Dead Letter Queue para análise/retry posterior
            if hasattr(self.orchestrator, "dlq") and self.orchestrator.dlq is not None:
                await self.orchestrator.dlq.push(
                    self.orchestrator.dlq.create_failed_task(
                        task_type="search",
                        payload={
                            "query": node.query,
                            "node_id": node.id,
                            "depth": node.depth,
                        },
                        error=str(e),
                        source="deep_researcher._search_for_node",
                    )
                )
            return []

    async def _generate_hypotheses(
        self, query: str, parent_results: list[SearchResult]
    ) -> list[str]:
        """
        Uses the LLM to generate competing hypotheses to explore.
        Enriches the prompt with graph/vector context from OrvixMemoryV2
        when available, surfacing related past research automatically.
        """
        context_snippets = "\n".join(
            f"- {r.title or '(sem título)'}: {(r.description or '')[:80]}"
            for r in parent_results[:5]
        )

        # ── Contexto do RAG Híbrido (OrvixMemoryV2) ───────────────────────
        memory_context = ""
        if self.memory is not None:
            try:
                memory_context = self.memory.get_context(query, top_k=3)
                if memory_context:
                    logger.debug(
                        f"DeepResearcher: contexto de memória recuperado "
                        f"({len(memory_context)} chars) para query='{query[:40]}'"
                    )
            except Exception as e:
                logger.warning(
                    f"DeepResearcher: falha ao recuperar contexto de memória: {e}"
                )

        memory_section = (
            f"\n\nRelated past research (from memory graph):\n{memory_context}"
            if memory_context
            else ""
        )

        prompt = (
            f"You are a research strategist generating competing hypotheses to investigate.\n\n"
            f"Original query: {query}\n\n"
            f"Results found so far:\n{context_snippets or '(none yet)'}"
            f"{memory_section}\n\n"
            f"Generate {self.MAX_BRANCHES} distinct, specific, testable hypotheses or sub-queries "
            f"representing competing or diverging angles (such as confirmative proof, contrasting/critical views, and alternative paradigms) "
            f"that would help answer the original query from different perspectives.\n"
            f"Return ONLY a JSON array of strings, e.g.:\n"
            f'["confirmative: hypothesis 1", "contrasting: hypothesis 2", "alternative: hypothesis 3", "hypothesis 4"]\n'
            f"Prefix each hypothesis with its angle (confirmative:, contrasting:, or alternative:) if applicable."
            f"Each hypothesis should be a search query, not a sentence."
        )

        schema = {"type": "array", "items": {"type": "string"}}

        try:
            await self._track_llm_call(prompt)  # Contabiliza tokens e custo estimado
            hypotheses = await self.llm.generate_structured(
                prompt, schema, temperature=0.4
            )
            if isinstance(hypotheses, list):
                return [str(h) for h in hypotheses if h][: self.MAX_BRANCHES]
        except Exception as e:
            logger.warning(f"Hypothesis generation failed: {e}")

        return [
            f"{query} best practices",
            f"{query} alternatives comparison",
            f"{query} real-world usage examples",
            f"{query} performance benchmarks 2026",
        ]

    def _estimate_confidence(self, results: list[SearchResult]) -> float:
        """Estimates node confidence from the average confidence_score of results."""
        if not results:
            return 0.0
        total = sum(getattr(r, "confidence_score", 0.0) for r in results)
        return round(min(1.0, total / len(results)), 3)

    def _consolidate_tree(self, root: ResearchNode) -> list[SearchResult]:
        """Collects all SearchResults from confirmed and explored nodes."""
        collected: list[SearchResult] = []
        seen_urls: set = set()

        def _walk(node: ResearchNode) -> None:
            if node.status in (
                "dead_end",
                "pruned_low_confidence",
                "pruned_beam",
                "pruned_budget",
            ):
                return
            for r in node.results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    collected.append(r)
            for child in node.children:
                _walk(child)

        _walk(root)
        collected.sort(key=lambda r: getattr(r, "confidence_score", 0.0), reverse=True)
        return collected

    def _export_tree_as_markdown(self, root: ResearchNode) -> str:
        """Exports the reasoning tree as readable markdown."""
        lines: list[str] = ["## Reasoning Tree", ""]

        # Injeta um resumo das hipóteses concorrentes
        lines.append("### Competing Hypotheses Summary")
        lines.append("| Hypothesis / Sub-query | Angle | Status | Confidence |")
        lines.append("|---|---|---|---|")

        def _collect_table_rows(node: ResearchNode):
            if node.depth > 0:
                status_str = node.status.replace("_", " ").capitalize()
                lines.append(
                    f"| {node.hypothesis[:60]} | {node.angle.capitalize()} | {status_str} | {node.confidence:.2%} |"
                )
            for child in node.children:
                _collect_table_rows(child)

        _collect_table_rows(root)
        lines.append("")

        status_icons = {
            "confirmed": "✅",
            "explored": "🔍",
            "dead_end": "❌",
            "pending": "⏳",
            "pruned_low_confidence": "⚠️ (poda-confiança)",
            "pruned_beam": "⚪ (poda-beam)",
            "pruned_budget": "🛑 (poda-orçamento)",
        }

        angle_icons = {
            "confirmative": "👍",
            "contrasting": "👎",
            "alternative": "💡",
            "general": "🔍",
        }

        def _render(node: ResearchNode, prefix: str, is_last: bool) -> None:
            connector = "└── " if is_last else "├── "
            icon = status_icons.get(node.status, "❓")
            angle_icon = angle_icons.get(node.angle, "🔍")
            label = node.hypothesis[:80] if node.hypothesis else node.query[:80]
            conf = f"[conf={node.confidence:.2f}]" if node.confidence > 0 else ""
            lines.append(f"{prefix}{connector}{icon} {angle_icon} {label} {conf}")

            if node.reasoning:
                detail_prefix = prefix + ("    " if is_last else "│   ")
                lines.append(f"{detail_prefix}   _{node.reasoning}_")

            child_prefix = prefix + ("    " if is_last else "│   ")
            for i, child in enumerate(node.children):
                _render(child, child_prefix, i == len(node.children) - 1)

        icon = status_icons.get(root.status, "❓")
        lines.append(f"### {icon} Root: {root.query}")
        if root.reasoning:
            lines.append(f"_{root.reasoning}_")
        lines.append("")

        for i, child in enumerate(root.children):
            _render(child, "", i == len(root.children) - 1)

        lines.append("")
        return "\n".join(lines)

    def _collect_by_status(self, root: ResearchNode, status: str) -> list[str]:
        """Collects all hypothesis strings for nodes matching the given status."""
        collected: list[str] = []

        def _walk(node: ResearchNode) -> None:
            if node.status == status:
                collected.append(node.hypothesis or node.query)
            for child in node.children:
                _walk(child)

        _walk(root)
        return collected

    def _count_nodes(self, root: ResearchNode) -> int:
        """Counts total nodes in the tree."""
        return 1 + sum(self._count_nodes(c) for c in root.children)
