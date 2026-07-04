"""Servico de raciocinio que encapsula analise de intencao, expansao de queries, ranqueamento e debate entre LLMs."""

import logging
from datetime import datetime
from statistics import mean
from typing import Any

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
        """Delega ao `IntentAnalyzer` do orquestrador."""
        return self.orch.intent_analyzer

    @property
    def query_expander(self):
        """Delega ao `QueryExpander` do orquestrador."""
        return self.orch.query_expander

    @property
    def ranker(self):
        """Delega ao `QualityRanker` do orquestrador."""
        return self.orch.ranker

    @property
    def confidence_scorer(self):
        """Delega ao `ConfidenceScorerV2` do orquestrador."""
        return self.orch.confidence_scorer

    @property
    def gap_detector(self):
        """Delega ao `GapDetector` do orquestrador."""
        return self.orch.gap_detector

    @property
    def sanitizer(self):
        """Delega ao `LLMSanitizer` do orquestrador."""
        return self.orch.sanitizer

    @property
    def conflict_detector(self):
        """Delega ao `ConflictDetector` do orquestrador."""
        return self.orch.conflict_detector

    @property
    def peer_reviewer(self):
        """Delega ao `PeerReviewAgent` do orquestrador."""
        return self.orch.peer_reviewer

    @property
    def llm(self):
        """Delega ao `LLMClient` do orquestrador."""
        return self.orch.llm

    @property
    def searchers(self):
        """Delega ao mapa de searchers do orquestrador."""
        return self.orch.searchers

    @property
    def operation_mode(self):
        """Delega ao modo de operacao ativo do orquestrador."""
        return self.orch.operation_mode

    async def analyze_intent(self, query: str) -> Any:
        """Analisa a intencao da query delegando ao `IntentAnalyzer`."""
        return await self.intent_analyzer.analyze(query)

    async def expand_queries(self, query: str, intent) -> list[Any]:
        """Expande a query delegando ao `QueryExpander`."""
        return await self.query_expander.expand(query, intent)

    async def analyze_and_expand(
        self, query: str, context_query: str | None = None
    ) -> tuple[Any, list[Any]]:
        """
        Analisa a intenção e expande as consultas de forma consolidada em uma única chamada.
        Delega ao `IntentAnalyzer.analyze_and_expand()`.
        """
        return await self.intent_analyzer.analyze_and_expand(
            query, context_query=context_query
        )

    async def rank(self, results: list[Any], query: str | None = None) -> list[Any]:
        """
        Ranqueia os resultados combinando heuristica por fonte + BM25 +
        embeddings (ranking hibrido, sem LLM) via `QualityRanker.rank()`.

        Quando `query` e informado, o proprio `QualityRanker` delega ao
        `HybridRanker` (ver `src/ranking/hybrid_ranker.py`), que ja inclui o
        re-ranking semantico via `SemanticReranker`. Por isso nao ha mais um
        passo separado de re-ranking aqui: chamar o modelo de embeddings
        duas vezes (uma dentro do ranking hibrido, outra aqui) dobraria o
        custo dessa etapa sem nenhum ganho de qualidade.
        """
        return await self.ranker.rank(results, query=query)

    async def calculate_overall_confidence(self, results: list[Any]) -> float:
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
        return round(
            min(base_confidence * diversity_bonus * (0.5 + 0.5 * quality_ratio), 1.0), 4
        )

    async def run_debate_mode(
        self, query: str, start_time: datetime, formats: list[Any] | None = None
    ) -> str:
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
                logger.warning(
                    f"OrvixMemory.store_research_result falhou para o debate: {e}"
                )

        return report
