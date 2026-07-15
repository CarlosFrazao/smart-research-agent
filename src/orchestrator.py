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

        # Inicialização do StreamMonitorAgent (opcional)
        self.stream_monitor = None
        if self.config.enable_live_monitoring:
            from src.stream_monitor_agent import StreamMonitorAgent

            self.stream_monitor = StreamMonitorAgent(
                knowledge_graph=getattr(self, "knowledge_graph", None),
                github_token=self.config.github_token,
            )
            for feed in self.config.monitoring_feeds:
                self.stream_monitor.add_feed(**feed)
            logger.info(
                "StreamMonitorAgent inicializado com %d feeds.",
                len(self.config.monitoring_feeds),
            )

        # Inicialização do HITLDialogAgent
        from src.hitl_dialog_agent import HITLDialogAgent

        self.hitl_dialog = HITLDialogAgent(
            hitl_manager=getattr(self, "hitl_manager", None),
            llm=getattr(self, "llm", None),
            dialog_callback=getattr(self, "_push_dialog_to_sse", None),
        )
        logger.info("HITLDialogAgent inicializado.")

        # Monta o pipeline de estágios independentes usado por `research()`
        self._pipeline = StageFactory.build_pipeline(self)

        logger.info("Orchestrator inicializado")

    async def research(
        self,
        query: str,
        formats: list[Any] | None = None,
        progress_callback: Optional[ProgressCallback] = None,
        session_id: str = "default_session",
        context_extra: dict | None = None,
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

        # Iniciar workers do monitor se ainda não estiverem rodando
        if self.stream_monitor and not getattr(self.stream_monitor, "_running", False):
            try:
                await self.stream_monitor.start()
            except Exception as e:
                logger.warning(f"Falha ao iniciar StreamMonitorAgent: {e}")
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
            if context_extra:
                context.extras.update(context_extra)
            try:
                context = await self._pipeline.run(context)
            finally:
                # FASE 5: estimativa de custo final para o painel de transparência.
                # Calculada sob demanda a partir do SourcePlan e do TokenEconomy;
                # nunca quebra o pipeline se indisponível (best-effort).
                try:
                    from src.pipeline.stages.expand_stage import estimate_search_cost
                    from src.token_economy import TokenEconomy

                    if context.source_plan is not None:
                        te = TokenEconomy()
                        n_queries = max(len(context.expanded_queries), 1)
                        est = estimate_search_cost(
                            context.source_plan, te, n_queries=n_queries
                        )
                        context.extra["estimated_cost_usd"] = est
                except Exception as cost_exc:  # noqa: BLE001 - best-effort
                    logger.debug(
                        "FASE 5: não foi possível estimar custo para transparência: %s",
                        cost_exc,
                    )
                # Exponha o contexto final para a UI (transparência de busca) e
                # ferramentas que queiram inspecionar ranked_results/custo sem
                # acoplar o Orchestrador ao protocolo do PipelineContext.
                self.last_context = context
                await self.close_searchers()
                # Bloco 10 (E7-T1): registra a pesquisa no Audit Log (best-effort).
                self._audit_research(context)
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

    def _audit_research(self, context: Any) -> None:
        """Registra a pesquisa concluída no Audit Log (Bloco 10 / E7-T1).

        Best-effort: nunca levanta. Lê modo, fontes, score RAGAS e estimativa
        de tokens do contexto/operação quando disponíveis.
        """
        try:
            from src.audit_log import get_audit_logger

            mode = ""
            operation_mode = getattr(self, "operation_mode", None)
            if operation_mode is not None:
                mode = getattr(operation_mode, "name", "") or ""
            if not mode:
                mode = (
                    getattr(getattr(self, "config", None), "operation_mode", "") or ""
                )

            sources_used = list(getattr(self, "searchers", {}).keys())

            ragas_score = None
            qg = context.extra.get("quality_gate_result") if context else None
            if qg is not None:
                ragas_score = getattr(qg, "faithfulness", None)

            token_estimate = None
            try:
                token_estimate = getattr(context, "estimated_cost_usd", None)
            except Exception:  # pragma: no cover - defensivo
                token_estimate = None

            get_audit_logger().log_research(
                query=context.query,
                mode=mode,
                sources_used=sources_used,
                ragas_score=ragas_score,
                token_estimate=token_estimate,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("AuditLogger: falha ao registrar pesquisa: %s", exc)

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
        # Parar monitor de streams se estiver ativo
        if getattr(self, "stream_monitor", None) and getattr(
            self.stream_monitor, "_running", False
        ):
            try:
                await self.stream_monitor.stop()
            except Exception as e:
                logger.warning(f"Erro ao parar StreamMonitorAgent: {e}")

        if hasattr(self, "knowledge_graph") and self.knowledge_graph:
            await self.knowledge_graph.close()
        if self.memory:
            try:
                self.memory.close()
            except Exception as e:
                logger.warning(f"Erro ao fechar OrvixMemoryV2: {e}")

    async def _apply_hitl_decision(self, decision: dict, context: Any) -> None:
        """Aplica a decisão do usuário (HITL) no contexto do pipeline."""
        if not decision:
            return
        action = decision.get("action")
        data = decision.get("parameters", decision.get("data", {}))

        if action == "pivot_to_contradiction" or action == "pivot":
            logger.info(f"HITL Decision: Pivô solicitado. Novo foco: {data}")
            if isinstance(data, dict) and data.get("additional_query"):
                from src.types import ExpandedQuery

                eq = ExpandedQuery(
                    query=data["additional_query"],
                    type="hitl_pivot",
                    priority="alta",
                )
                if eq not in context.expanded_queries:
                    context.expanded_queries.append(eq)
            elif isinstance(data, str) and data:
                from src.types import ExpandedQuery

                eq = ExpandedQuery(query=data, type="hitl_pivot", priority="alta")
                if eq not in context.expanded_queries:
                    context.expanded_queries.append(eq)
        elif action in ("exclude_source", "veto"):
            source_to_exclude = (
                data.get("source") if isinstance(data, dict) else str(data)
            )
            if source_to_exclude:
                # Filtrar resultados da fonte vetada de ranked_results.
                original_count = len(context.ranked_results)
                context.ranked_results = [
                    r
                    for r in context.ranked_results
                    if getattr(r, "source", None) != source_to_exclude
                ]
                removed = original_count - len(context.ranked_results)
                logger.info(
                    "HITL veto applied: removed %d results from source '%s'",
                    removed,
                    source_to_exclude,
                )
                # Registrar como sinal negativo no feedback_store (se disponível).
                if hasattr(self, "feedback_store") and self.feedback_store:
                    try:
                        self.feedback_store.record(
                            user_id=getattr(context, "user_id", "anonymous"),
                            query=context.query,
                            result_id=f"hitl_veto:{source_to_exclude}",
                            signal="not_useful",
                            source_name=source_to_exclude,
                        )
                    except Exception:
                        logger.debug(
                            "feedback_store.record falhou silenciosamente durante HITL veto",
                            exc_info=True,
                        )
            else:
                logger.info("HITL veto recebido sem fonte definida; ignorado.")
        elif action in ("expand_scope", "expand"):
            expand_hint = data.get("hint") if isinstance(data, dict) else str(data)
            logger.info("HITL expand_scope triggered with hint: %s", expand_hint)
            # Adicionar hint ao contexto para que stages subsequentes possam usar.
            if not hasattr(context, "expand_hints"):
                context.expand_hints = []
            context.expand_hints.append(expand_hint)
            # Limitação conhecida (por design): a re-execução completa do
            # SearchStage com sources adicionais não é acionada aqui. O hint é
            # registrado em context.expand_hints e os stages subsequentes o
            # consomem; o pipeline prossegue com os resultados já coletados,
            # evitando uma segunda passada de busca cara no meio do fluxo HITL.
            logger.warning(
                "HITL expand_scope: hint registrado; re-busca completa não é "
                "acionada aqui (limitação conhecida). O contexto usará os "
                "resultados existentes."
            )
        else:
            logger.info(f"HITL Decision: Ação padrão (incluir/ignorar): {action}")


# Atributos de compatibilidade expostos para patches de testes legados
IntentAnalyzer = None
QueryExpander = None
SourcePlanner = None
QualityRanker = None
GapDetector = None
Synthesizer = None
ReportGenerator = None
Cache = None
