"""Orquestrador central do Smart Research Agent (SRA).

Facade fina que delega:
  - inicialização de dependências para `StageFactory`
    (src/pipeline/stage_factory.py)
  - registro de fallbacks de saúde para `FallbackManager`
    (src/pipeline/fallback_manager.py)
  - execução do fluxo de pesquisa para um `Pipeline` de estágios
    independentes (src/pipeline/pipeline.py, src/pipeline/stages.py)

O `Orchestrator` continua sendo o ponto de entrada público e o objeto de
contexto compartilhado pelos serviços especializados (SearchService,
ReasoningService, MemoryService, ReportService) — eles recebem `self` no
construtor e leem atributos como `orchestrator.llm` ou `orchestrator.searchers`
diretamente — mas não concentra mais a lógica de wiring nem o passo-a-passo da
pesquisa. Essa lógica agora vive in módulos dedicados e testáveis
isoladamente, resolvendo o problema de God Object da versão anterior.
"""

from __future__ import annotations

from src.clients.llm_client import LLMClient
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.clients.llm_client import LLMClient
    from src.memory.orvix_memory import OrvixMemoryV2
    from src.memory.knowledge_graph import KnowledgeGraph
    from src.intent_analyzer import IntentAnalyzer
    from src.query_expander import QueryExpander
    from src.source_planner import SourcePlanner
    from src.ranker import QualityRanker
    from src.confidence_scorer import ConfidenceScorerV2
    from src.gap_detector import GapDetector
    from src.synthesizer import Synthesizer
    from src.report_generator import ReportGenerator
    from src.link_verifier import LinkVerifier
    from src.research_score import ResearchScoreAggregator
    from src.conflict_detector import ConflictDetector
    from src.peer_review_agent import PeerReviewAgent
    from src.search.semantic_reranker import SemanticReranker
    from src.security.llm_sanitizer import LLMSanitizer
    from src.cache import Cache
    from src.utils.dead_letter_queue import DeadLetterQueue
    from src.monitoring.health_monitor import HealthMonitor
    from src.services.search_service import SearchService
    from src.services.reasoning_service import ReasoningService
    from src.services.memory_service import MemoryService
    from src.services.report_service import ReportService

from src.config import Config
from src.pipeline.fallback_manager import FallbackManager
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stage_factory import StageFactory
from src.utils.logging import setup_logger
from src.monitoring.tracing import ensure_correlation_id, trace_async_span

import inspect
from typing import Awaitable, Callable, Optional, Union

# Assinatura do callback de progresso: (etapa_atual, total_etapas, mensagem) -> None | Awaitable[None]
ProgressCallback = Callable[[int, int, str], Union[None, Awaitable[None]]]

logger = setup_logger("orchestrator")


class Orchestrator:
    """
    Facade principal do Smart Research Agent (SRA).

    Delega inicialização (`StageFactory`), resiliência (`FallbackManager`) e
    execução do pipeline (`Pipeline` + `PipelineStage`s) para componentes
    dedicados, mantendo apenas a coordenação de alto nível e a
    compatibilidade com o código legado que acessa `orchestrator.<componente>`
    diretamente.
    """

    # Anotações de tipo de classe para checagem estática (evita warnings de missing-attribute)
    config: Config
    llm: LLMClient
    memory: OrvixMemoryV2 | None
    knowledge_graph: KnowledgeGraph
    intent_analyzer: IntentAnalyzer
    query_expander: QueryExpander
    source_planner: SourcePlanner
    ranker: QualityRanker
    confidence_scorer: ConfidenceScorerV2
    gap_detector: GapDetector
    synthesizer: Synthesizer
    report_generator: ReportGenerator
    link_verifier: LinkVerifier
    score_aggregator: ResearchScoreAggregator
    conflict_detector: ConflictDetector
    peer_reviewer: PeerReviewAgent
    evidence_graph: Any
    semantic_reranker: SemanticReranker
    sanitizer: LLMSanitizer
    searchers: dict[str, Any]
    cache: Cache
    smart_cache: Cache
    dlq: DeadLetterQueue
    operation_mode: Any
    auditor: Any
    health_monitor: HealthMonitor

    _search_service: SearchService
    _reasoning_service: ReasoningService
    _memory_service: MemoryService
    _report_service: ReportService

    # Numero total de checkpoints granulares reportados durante `research()`.
    TOTAL_PROGRESS_STEPS = 13

    async def _report_progress(
        self,
        callback: Optional[ProgressCallback],
        step: int,
        message: str,
    ) -> None:
        """Notifica o `callback` de progresso, se fornecido, sem jamais interromper o pipeline.

        O callback pode ser sincrono ou assincrono. Qualquer excecao lancada por
        ele e apenas logada (nunca propagada).
        """
        if callback is None:
            return
        try:
            result = callback(step, self.TOTAL_PROGRESS_STEPS, message)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.debug(f"progress_callback falhou (ignorado): {e}")

    def __init__(self, config: Config = None):
        self.config = config or Config()

        # Wiring de todas as dependências (LLM, memória, searchers, serviços...)
        StageFactory.initialize_components(self, self.config)

        # Registra callbacks de fallback do HealthMonitor
        FallbackManager(self).register_all()

        # Monta o pipeline de estágios independentes usado por `research()`
        self._pipeline = StageFactory.build_pipeline(self)

        logger.info("Orchestrator inicializado")

    async def research(
        self,
        query: str,
        formats: list[Any] | None = None,
        progress_callback: Optional[ProgressCallback] = None,
        session_id: str = "default_session",
    ) -> str:
        """Executa o pipeline completo de pesquisa e retorna o relatorio Markdown.

        Etapas: health check -> planejamento -> busca -> ranqueamento ->
        conflitos -> gap-fill -> sanitização -> sintese -> relatorio.
        Cada etapa é um `PipelineStage` independente em `src/pipeline/stages.py`.

        Args:
            query: Pergunta ou topico a ser pesquisado.
            formats: Formatos de exportacao adicionais alem do Markdown padrao
                (ex: `[ReportFormat.PDF]`). Opcional.
            progress_callback: Callback opcional de progresso (ver SSE/streaming).
            session_id: ID unico da sessao para rastreamento / HITL.

        Returns:
            str: Relatorio completo em formato Markdown.
        """
        correlation_id = ensure_correlation_id()
        logger.info(
            f"Iniciando pesquisa: '{query}' [modo: {self.operation_mode.name}] "
            f"[correlation_id={correlation_id}]"
        )
        if getattr(self.operation_mode, "enable_debate", False):
            from datetime import datetime

            return await self.reasoning.run_debate_mode(
                query, datetime.now(), formats=formats
            )

        await self._report_progress(progress_callback, 0, "Iniciando pesquisa")
        async with trace_async_span(
            "research.pipeline",
            {
                "sra.query_preview": query[:200],
                "sra.mode": self.operation_mode.name,
            },
        ):
            context = PipelineContext(query=query, formats=formats)
            context.extras["progress_callback"] = progress_callback
            context.extras["orchestrator"] = self
            context.extras["session_id"] = session_id
            try:
                context = await self._pipeline.run(context)
            finally:
                await self.close_searchers()
            await self._report_progress(
                progress_callback, self.TOTAL_PROGRESS_STEPS, "Pesquisa concluida"
            )
            return context.report

    async def close_searchers(self) -> None:
        """Fecha as sessões e recursos de todos os buscadores ativos de forma assíncrona."""
        if hasattr(self, "searchers") and self.searchers:
            for name, searcher in self.searchers.items():
                if hasattr(searcher, "close") and callable(searcher.close):
                    try:
                        await searcher.close()
                    except Exception as e:
                        logger.debug(f"Erro ao fechar searcher {name}: {e}")

    # ── Retrocompatibilidade / Delegados do Facade ───────────────────────
    # Mantidos para não quebrar chamadores externos (e testes) que ainda
    # usam os nomes antigos com underscore. Toda a lógica real vive nos
    # Services (search/reasoning/memory_service/reports).

    async def _select_scraper_for_url(self, url: str) -> list[Any]:
        """Retrocompatibilidade: delega para `SearchService.select_scraper_for_url`."""
        return await self.search.select_scraper_for_url(url)

    async def _parallel_search(self, queries: list[Any], plan: Any, intent: Any):
        """Retrocompatibilidade: delega para `SearchService.execute`."""
        return await self.search.execute(queries, plan, intent)

    async def _search_task(
        self, searcher: Any, source_name: str, query: str, domain: str
    ):
        """Retrocompatibilidade: delega para `SearchService._search_task`."""
        return await self.search._search_task(searcher, source_name, query, domain)

    async def _search_with_timeout(self, searcher: Any, query: str, domain: str):
        """Retrocompatibilidade: delega para `SearchService._search_with_timeout`."""
        return await self.search._search_with_timeout(searcher, query, domain)

    async def _calculate_overall_confidence(self, results: list) -> float:
        """Retrocompatibilidade: delega para `ReasoningService.calculate_overall_confidence`."""
        return await self.reasoning.calculate_overall_confidence(results)

    def _init_searchers(self) -> dict[str, Any]:
        """Retrocompatibilidade: re-executa a instanciação de searchers via `SearcherFactory`."""
        from src.search.factory import SearcherFactory

        return SearcherFactory.create_searchers(self)

    def _register_health_fallbacks(self) -> None:
        """Retrocompatibilidade: delega para `FallbackManager.register_all`.

        Não é mais chamado automaticamente no `__init__` (isso já acontece via
        `FallbackManager(self).register_all()`); mantido apenas para código
        legado que o invocava diretamente.
        """
        FallbackManager(self).register_all()

    # ── Lazy-loaded Service Properties ─────────────────────────────────────

    @property
    def search(self):
        """SearchService lazy-loaded para execucao de buscas paralelas."""
        if not hasattr(self, "_search_service") or self._search_service is None:
            from src.services.search_service import SearchService

            self._search_service = SearchService(self)
        return self._search_service

    @search.setter
    def search(self, value):
        self._search_service = value

    @property
    def reasoning(self):
        """ReasoningService lazy-loaded para analise de intencao e ranqueamento."""
        if not hasattr(self, "_reasoning_service") or self._reasoning_service is None:
            from src.services.reasoning_service import ReasoningService

            self._reasoning_service = ReasoningService(self)
        return self._reasoning_service

    @reasoning.setter
    def reasoning(self, value):
        self._reasoning_service = value

    @property
    def memory_service(self):
        """MemoryService lazy-loaded para contexto de memoria persistente."""
        if not hasattr(self, "_memory_service") or self._memory_service is None:
            from src.services.memory_service import MemoryService

            self._memory_service = MemoryService(self)
        return self._memory_service

    @memory_service.setter
    def memory_service(self, value):
        self._memory_service = value

    @property
    def reports(self):
        """ReportService lazy-loaded para geracao e salvamento de relatorios."""
        if not hasattr(self, "_report_service") or self._report_service is None:
            from src.services.report_service import ReportService

            self._report_service = ReportService(self)
        return self._report_service

    @reports.setter
    def reports(self, value):
        self._report_service = value

    async def close(self) -> None:
        """Encerra pools de conexoes e recursos da memoria e do Grafo de Conhecimento."""
        if hasattr(self, "knowledge_graph") and self.knowledge_graph:
            await self.knowledge_graph.close()
        if self.memory:
            try:
                self.memory.close()
            except Exception as e:
                logger.warning(f"Erro ao fechar OrvixMemoryV2: {e}")


# Atributos de compatibilidade expostos para patches de testes legados
IntentAnalyzer = None
QueryExpander = None
SourcePlanner = None
QualityRanker = None
GapDetector = None
Synthesizer = None
ReportGenerator = None
Cache = None
