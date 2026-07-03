import logging
from statistics import mean
from datetime import datetime
from typing import List, Any, Optional

logger = logging.getLogger("orchestrator.reasoning_service")


class ReasoningService:
    """
    Orquestra a análise de intenção, expansão de consultas, ranking de qualidade
    e execução do modo debate científico.
    """

    def __init__(self, orchestrator):
        self.orch = orchestrator

    @property
    def intent_analyzer(self):
        return self.orch.intent_analyzer

    @property
    def query_expander(self):
        return self.orch.query_expander

    @property
    def ranker(self):
        return self.orch.ranker

    @property
    def confidence_scorer(self):
        return self.orch.confidence_scorer

    @property
    def gap_detector(self):
        return self.orch.gap_detector

    @property
    def sanitizer(self):
        return self.orch.sanitizer

    @property
    def conflict_detector(self):
        return self.orch.conflict_detector

    @property
    def peer_reviewer(self):
        return self.orch.peer_reviewer

    @property
    def llm(self):
        return self.orch.llm

    @property
    def searchers(self):
        return self.orch.searchers

    @property
    def operation_mode(self):
        return self.orch.operation_mode

    async def analyze_intent(self, query: str) -> Any:
        return await self.intent_analyzer.analyze(query)

    async def expand_queries(self, query: str, intent) -> List[Any]:
        return await self.query_expander.expand(query, intent)

    async def rank(self, results: List[Any], query: Optional[str] = None) -> List[Any]:
        """
        Ranqueia os resultados pelo score de qualidade clássico (ranker).
        Se `query` for informado, aplica re-ranking semântico em seguida via SemanticReranker.
        """
        ranked = await self.ranker.rank(results)
        if query and hasattr(self.orch, "semantic_reranker"):
            try:
                # Converte objetos SearchResult para dict para o reranker
                as_dicts = [r.__dict__ if hasattr(r, "__dict__") else dict(r) for r in ranked]
                reranked_dicts = await self.orch.semantic_reranker.rerank(query, as_dicts)
                # Recria a ordem na lista original (preserva objetos originais, apenas reordena)
                url_to_obj = {
                    (r.url if hasattr(r, "url") else r.get("url", "")): r
                    for r in ranked
                }
                ranked = [
                    url_to_obj.get(d.get("url", ""), ranked[i])
                    for i, d in enumerate(reranked_dicts)
                ]
            except Exception as e:
                logger.warning(f"ReasoningService.rank: SemanticReranker falhou ({e}), mantendo ranking original")
        return ranked


    async def calculate_overall_confidence(self, results: List[Any]) -> float:
        """
        Calcula a confiança geral com base nos scores individuais,
        diversidade de fontes e qualidade relativa dos resultados.
        """
        if not results:
            return 0.0
        individual_scores = [
            r.confidence_score
            for r in results
            if hasattr(r, "confidence_score") and r.confidence_score is not None
        ]
        if not individual_scores:
            return 0.5
        source_diversity = len(set(getattr(r, "source", "") for r in results))
        diversity_bonus = min(source_diversity / 5, 1.0)
        high_quality = sum(1 for s in individual_scores if s > 0.7)
        quality_ratio = high_quality / len(individual_scores)
        base_confidence = mean(individual_scores)
        return round(min(base_confidence * diversity_bonus * (0.5 + 0.5 * quality_ratio), 1.0), 4)

    async def run_debate_mode(self, query: str, start_time: datetime, formats: Optional[List[Any]] = None) -> str:
        """
        Executa o debate científico entre duas hipóteses concorrentes e gera o relatório veredito.
        """
        logger.info("Modo DEBATE ativo. Iniciando DebateOrchestrator...")
        from src.debate_orchestrator import DebateOrchestrator
        debate = DebateOrchestrator(llm_client=self.llm, searchers=self.searchers)
        debate_round = await debate.run(query)
        report = debate.format_debate_markdown(debate_round)

        # Salvar e sincronizar via ReportService
        duration = (datetime.now() - start_time).total_seconds()
        filepath = self.orch.reports.save(report, query, formats=formats)
        logger.info(f"Debate completo em {round(duration, 1)}s. Relatorio: {filepath}")

        # Obsidian sync via ReportService
        self.orch.reports.sync_to_vault(filepath)

        # Armazenar em memória via MemoryService
        if self.orch.memory:
            try:
                self.orch.memory.store_research_result(
                    query=query,
                    executive_summary=f"Debate vencedor: {debate_round.winner}. Veredito: {debate_round.verdict}",
                    top_entities=[debate_round.winner] if debate_round.winner else [],
                    domain="general",
                    duration_seconds=duration,
                )
            except Exception as e:
                logger.warning(f"OrvixMemory.store_research_result falhou para o debate: {e}")

        return report
