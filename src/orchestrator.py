"""Orquestrador central do Smart Research Agent (SRA).

Facade que coordena o pipeline completo de pesquisa:
planejamento -> busca paralela -> ranqueamento -> sintese -> relatorio.
Delega cada etapa a servicos especializados (SearchService, ReasoningService,
MemoryService, ReportService) mantendo o `Orchestrator` como ponto de entrada.
"""

from datetime import datetime
from typing import Any

from src.clients.llm_client import LLMClient, LLMProvider
from src.clients.smart_model_router import get_router
from src.confidence_scorer import ConfidenceScorerV2
from src.config import Config
from src.conflict_detector import ConflictDetector
from src.evidence_graph import EvidenceGraph
from src.gap_detector import GapDetector
from src.intent_analyzer import IntentAnalyzer
from src.link_verifier import LinkVerifier
from src.memory.orvix_memory import OrvixMemoryV2
from src.monitoring.health_monitor import HealthMonitor
from src.operation_modes import OperationConfig, OperationModes
from src.peer_review_agent import PeerReviewAgent
from src.query_expander import QueryExpander
from src.ranker import QualityRanker
from src.report_generator import ReportGenerator
from src.research_auditor import ResearchAuditor
from src.research_score import ResearchScoreAggregator
from src.search.factory import SearcherFactory
from src.search.semantic_reranker import SemanticReranker
from src.services.memory_service import MemoryService
from src.services.reasoning_service import ReasoningService
from src.services.report_service import ReportService

# Importações dos novos serviços
from src.services.search_service import SearchService
from src.source_planner import SourcePlanner
from src.synthesizer import Synthesizer
from src.types import ExpandedQuery, ResearchMetadata
from src.cache import Cache
from src.utils.dead_letter_queue import DeadLetterQueue
from src.utils.logging import setup_logger

logger = setup_logger("orchestrator")


class Orchestrator:
    """
    Facade principal do Smart Research Agent (SRA).
    Delega a execução para serviços especializados para manter a legibilidade e manutenibilidade.
    """

    def __init__(self, config: Config = None):
        self.config = config or Config()

        llm_config = self.config.get_llm_config()

        router = None
        if getattr(self.config, "smart_routing_enabled", True):
            router = get_router(
                openrouter_api_key=getattr(self.config, "openrouter_api_key", None)
            )
            logger.info("SmartModelRouter ativo — roteamento de custo habilitado")

        self.llm = LLMClient(
            LLMProvider(self.config.llm_provider),
            llm_config,
            model_router=router,
            fallback_configs=self.config.get_all_llm_configs(),
        )

        self.memory: OrvixMemoryV2 | None = None
        if getattr(self.config, "memory_enabled", True):
            try:
                self.memory = OrvixMemoryV2(
                    db_path=getattr(self.config, "memory_db_path", None)
                )
                logger.info(
                    f"OrvixMemoryV2 ativa (RAG Híbrido): {self.memory._db_path}"
                )
            except Exception as e:
                logger.warning(
                    f"OrvixMemoryV2 falhou ao inicializar: {e} — pesquisa continua sem memória"
                )

        from src.memory.knowledge_graph import KnowledgeGraph

        self.knowledge_graph = KnowledgeGraph(self.config)

        self.intent_analyzer = IntentAnalyzer(self.llm)
        self.query_expander = QueryExpander(self.llm)
        self.source_planner = SourcePlanner()
        self.ranker = QualityRanker(self.llm)
        self.confidence_scorer = ConfidenceScorerV2(llm_client=self.llm)
        self.gap_detector = GapDetector(self.llm)
        self.synthesizer = Synthesizer(self.llm)
        self.report_generator = ReportGenerator(self.llm)
        self.link_verifier = LinkVerifier()
        self.score_aggregator = ResearchScoreAggregator()
        self.conflict_detector = ConflictDetector(llm_client=self.llm)
        self.peer_reviewer = PeerReviewAgent(llm_client=self.llm)
        self.evidence_graph = EvidenceGraph()
        # SemanticReranker — lazy: modelo carregado na primeira chamada de rerank()
        self.semantic_reranker = SemanticReranker()

        from src.security.llm_sanitizer import LLMSanitizer

        self.sanitizer = LLMSanitizer(self.llm)

        self.searchers = self._init_searchers()
        self.cache = Cache(cache_dir=self.config.cache_dir)

        # SmartCache — Redis → memória, TTL por tipo de fonte
        from src.cache import Cache as SmartCache

        redis_url = getattr(self.config, "redis_url", None)
        self.smart_cache = SmartCache(redis_url=redis_url)

        # Dead Letter Queue
        self.dlq = DeadLetterQueue(path=getattr(self.config, "dlq_path", "./.dlq"))

        # Modo de operação ativo
        mode_name = getattr(self.config, "operation_mode", OperationModes.DEFAULT_MODE)
        self.operation_mode: OperationConfig = OperationModes.get_mode(mode_name)
        logger.info(f"OperationMode ativo: '{self.operation_mode.name}'")

        # ResearchAuditor
        self.auditor = ResearchAuditor(
            llm_client=self.llm,
            orchestrator=self,
            confidence_scorer=self.confidence_scorer,
        )

        # HealthMonitor
        self.health_monitor = HealthMonitor()
        self.health_monitor.orchestrator = self
        self._register_health_fallbacks()

        # Instanciação dos novos Serviços (Facade)
        self._search_service = SearchService(self)
        self._reasoning_service = ReasoningService(self)
        self._memory_service = MemoryService(self)
        self._report_service = ReportService(self)

        logger.info("Orchestrator inicializado")

    def _register_health_fallbacks(self):
        """Registra callbacks de fallback no HealthMonitor para cada servico externo.

        Fallbacks registrados:
            - ``use_ephemeral_chroma``: Substitui ChromaDB por instancia em memoria.
            - ``disable_firecrawl``: Desabilita o Firecrawl se indisponivel.
            - ``fallback_to_duckduckgo``: Ativa WebSearcher se SearXNG cair.
            - ``disable_cache``: Desabilita o cache Redis em caso de falha.
        """

        def fallback_use_ephemeral_chroma(svc, result):
            if self.memory:
                try:
                    import chromadb

                    self.memory.chroma_client = chromadb.Client()
                    self.memory.chroma_collection = (
                        self.memory.chroma_client.get_or_create_collection(
                            "sra_memories"
                        )
                    )
                    logger.info(
                        "HealthMonitor Fallback: ChromaDB offline. Usando cliente efêmero em memória."
                    )
                except Exception as e:
                    logger.error(f"Erro no fallback do ChromaDB: {e}")

        def fallback_disable_firecrawl(svc, result):
            logger.warning(
                "HealthMonitor Fallback: Desabilitando Firecrawl. Usando Jina/Spider fallback."
            )
            if "firecrawl" in self.searchers:
                self.searchers["firecrawl"].enabled = False

        def fallback_to_duckduckgo(svc, result):
            logger.warning(
                "HealthMonitor Fallback: SearXNG offline. Priorizando WebSearcher."
            )
            if "web" in self.searchers:
                self.searchers["web"].enabled = True

        def fallback_disable_cache(svc, result):
            logger.warning(
                "HealthMonitor Fallback: Redis/Cache offline. Desabilitando cache."
            )
            if hasattr(self, "cache") and self.cache:
                self.cache.enabled = False

        self.health_monitor.register_fallback(
            "use_ephemeral_chroma", fallback_use_ephemeral_chroma
        )
        self.health_monitor.register_fallback(
            "disable_firecrawl", fallback_disable_firecrawl
        )
        self.health_monitor.register_fallback(
            "fallback_to_duckduckgo", fallback_to_duckduckgo
        )
        self.health_monitor.register_fallback("disable_cache", fallback_disable_cache)

    def _init_searchers(self) -> dict[str, Any]:
        """Instancia os searchers via `SearcherFactory`.

        Returns:
            dict[str, Any]: Mapa de nome-do-searcher para instancia.
        """
        return SearcherFactory.create_searchers(self)

    async def research(self, query: str, formats: list[Any] | None = None) -> str:
        """Executa o pipeline completo de pesquisa e retorna o relatorio Markdown.

        Etapas: health check -> planejamento -> busca -> ranqueamento -> sintese -> relatorio.

        Args:
            query: Pergunta ou topico a ser pesquisado.
            formats: Formatos de exportacao adicionais alem do Markdown padrao
                (ex: `[ReportFormat.PDF]`). Opcional.

        Returns:
            str: Relatorio completo em formato Markdown.
        """
        start_time = datetime.now()
        logger.info(f"Iniciando pesquisa: '{query}' [modo: {self.operation_mode.name}]")

        # ── Health Check Inicial ──────────────────────────────────────────────
        try:
            health = await self.health_monitor.check_all()
            if not health.is_healthy:
                logger.warning(
                    f"HealthMonitor: Serviços offline/degradados detectados: {health.alerts}"
                )
        except Exception as e:
            logger.warning(f"Falha ao executar health check: {e}")

        # Intercepta fluxo se o modo for debate
        if getattr(self.operation_mode, "enable_debate", False):
            return await self.reasoning.run_debate_mode(
                query, start_time, formats=formats
            )

        memory_context = self.memory_service.get_context(query)
        enriched_query = query
        if memory_context:
            enriched_query = f"{memory_context}\n\n---\n\nQuery atual: {query}"

        # 1. Planejamento
        intent, expanded_queries, source_plan = await self._plan_search(
            query, enriched_query
        )

        # 2. Execução das buscas
        ranked = await self._execute_searches(
            query, intent, expanded_queries, source_plan
        )

        # 3. Síntese e geração de relatório
        try:
            report = await self._synthesize_results(
                query, ranked, intent, source_plan, start_time, formats
            )
        finally:
            await self.close_searchers()

        return report

    async def close_searchers(self) -> None:
        """Fecha as sessões e recursos de todos os buscadores ativos de forma assíncrona."""
        if hasattr(self, "searchers") and self.searchers:
            for name, searcher in self.searchers.items():
                if hasattr(searcher, "close") and callable(searcher.close):
                    try:
                        await searcher.close()
                    except Exception as e:
                        logger.debug(f"Erro ao fechar searcher {name}: {e}")

    async def _plan_search(
        self, query: str, enriched_query: str
    ) -> tuple[Any, list[ExpandedQuery], Any]:
        """Executa as etapas de planejamento: analise de intencao, expansao de queries e plano de fontes.

        Args:
            query: Query original do usuario.
            enriched_query: Query enriquecida com contexto da memoria persistente.

        Returns:
            tuple: (IntentResult, list[ExpandedQuery], SourcePlan).
        """
        logger.info("Passo 1/9: Analisando intencao...")
        intent = await self.reasoning.analyze_intent(enriched_query)
        logger.info(
            f"  Dominio: {intent.domain.value}, Intencao: {intent.intention.value}"
        )

        logger.info("Passo 2/9: Expandindo queries...")
        expanded_queries = await self.reasoning.expand_queries(query, intent)
        logger.info(f"  {len(expanded_queries)} queries expandidas")

        logger.info("Passo 3/9: Planejando fontes...")
        source_plan = self.source_planner.plan(intent, expanded_queries)
        logger.info(f"  Primarias: {', '.join(source_plan.primary)}")

        return intent, expanded_queries, source_plan

    async def _execute_searches(
        self,
        query: str,
        intent: Any,
        expanded_queries: list[ExpandedQuery],
        source_plan: Any,
    ) -> list[Any]:
        """Executa buscas paralelas, ranqueia e puntua por confianca.

        Args:
            query: Query original do usuario.
            intent: Resultado da analise de intencao.
            expanded_queries: Lista de queries expandidas.
            source_plan: Plano de fontes gerado pelo `SourcePlanner`.

        Returns:
            list[Any]: Lista de `RankedResult` com scores de confianca calculados.
        """
        logger.info("Passo 4/9: Buscando em paralelo...")
        all_results = await self.search.execute(expanded_queries, source_plan, intent)
        self._last_all_results_count = len(all_results)
        self._last_all_results_sources = (
            list(set(r.source for r in all_results))
            if all_results
            else list(self.searchers.keys())
        )
        logger.info(f"  {self._last_all_results_count} resultados brutos")

        logger.info("Passo 5/9: Ranqueando e re-ranqueando semanticamente...")
        ranked = await self.reasoning.rank(all_results, query=query)

        logger.info("Passo 5b/9: Scoring de confianca e anti-hallucination...")
        scored = await self.confidence_scorer.score_batch(ranked, cross_validate=True)

        logger.info("Passo 5c/9: Verificacao concorrente de links citados...")
        scored = await self.link_verifier.verify_results(scored)

        threshold = self.operation_mode.confidence_threshold
        ranked = [r for r in scored if r.confidence_score >= threshold]
        logger.info(
            f"  {len(ranked)} resultados apos filtro de confianca (>= {threshold})"
        )

        logger.info("Passo 5d/9: Detectando conflitos entre fontes...")
        self._last_conflict_report = None
        try:
            conflict_report = self.conflict_detector.detect(ranked)
            self._last_conflict_report = conflict_report
            if conflict_report.has_critical:
                logger.warning(
                    f"ConflictDetector: {len(conflict_report.critical_conflicts)} conflito(s) crítico(s)"
                )
                conflict_results = await self.conflict_detector.resolve(
                    conflict_report, self
                )
                if conflict_results:
                    resolved_scored = await self.confidence_scorer.score_batch(
                        conflict_results, cross_validate=True
                    )
                    resolved_scored = await self.link_verifier.verify_results(
                        resolved_scored
                    )
                    resolved_filtered = [
                        r for r in resolved_scored if r.confidence_score >= threshold
                    ]

                    ranked.extend(resolved_filtered)
                    ranked.sort(
                        key=lambda x: getattr(x, "score", 0.0)
                        or getattr(x, "confidence_score", 0.0),
                        reverse=True,
                    )
                    logger.info(
                        f"ConflictDetector: {len(resolved_filtered)} resultados adicionais inseridos"
                    )
        except Exception as e:
            logger.warning(f"ConflictDetector falhou (não crítico): {e}")

        logger.info("Passo 6-7/9: Detectando gaps e re-pesquisando...")
        iteration = 0
        gap = None
        while iteration < self.operation_mode.max_depth:
            gap = await self.gap_detector.detect(ranked, query, intent)
            if gap.is_complete:
                logger.info("  Pesquisa considerada completa")
                break

            logger.info(
                f"  Gap detectado (iter {iteration + 1}): {gap.missing_aspects}"
            )
            from src.utils.logging import structured_logger

            structured_logger.log_gap(
                gap_description=", ".join(gap.missing_aspects),
                query_used=", ".join(gap.new_queries),
                iteration=iteration + 1,
            )
            gap_queries = [
                ExpandedQuery(
                    query=q, type="gap_fill", priority="alta", rationale="gap detection"
                )
                for q in gap.new_queries
            ]
            new_results = await self.search.execute(gap_queries, source_plan, intent)
            new_ranked = await self.reasoning.rank(new_results, query=query)
            ranked.extend(new_ranked)
            ranked.sort(key=lambda x: x.score, reverse=True)
            iteration += 1

        self._last_gap = gap
        self._last_iterations = iteration

        logger.info("Passo 7b/9: Sanitização anti-injection (LLMSanitizer)...")
        sanitized = await self.sanitizer.sanitize_batch(
            [r.description or "" for r in ranked]
        )
        safe_ranked = []
        for r, s in zip(ranked, sanitized):
            if s.risk_score < 0.7:
                r.description = s.cleaned
                safe_ranked.append(r)
            else:
                logger.warning(
                    f"LLMSanitizer bloqueou/filtrou resultado com alto risco ({s.risk_score}) de '{r.url[:50]}'"
                )

        return safe_ranked

    async def _synthesize_results(
        self,
        query: str,
        ranked: list[Any],
        intent: Any,
        source_plan: Any,
        start_time: datetime,
        formats: list[Any] | None,
    ) -> str:
        """Sintetiza os resultados ranqueados e monta o relatorio final.

        Executa: sintese de entidades -> calculo de score -> auditoria -> peer review
        -> geracao do relatorio -> salvamento em disco.

        Args:
            query: Query original do usuario.
            ranked: Lista de resultados ranqueados com scores de confianca.
            intent: Resultado da analise de intencao.
            source_plan: Plano de fontes utilizado na busca.
            start_time: Timestamp do inicio da pesquisa para calculo de duracao.
            formats: Formatos de exportacao adicionais alem do Markdown padrao.

        Returns:
            str: Relatorio completo em formato Markdown.
        """
        logger.info("Passo 8/9: Sintetizando resultados...")

        synthesized = await self.synthesizer.synthesize(ranked)
        logger.info(f"  {len(synthesized)} entidades sintetizadas")

        logger.info("Passo 9/9: Gerando relatorio...")
        duration = (datetime.now() - start_time).total_seconds()

        total_results = getattr(self, "_last_all_results_count", len(ranked))
        sources = getattr(
            self, "_last_all_results_sources", list(self.searchers.keys())
        )

        metadata = ResearchMetadata(
            query=query,
            domain=intent.domain.value,
            sources=sources,
            total_results=total_results,
            iterations=self._last_iterations + 1,
            timestamp=datetime.now(),
            duration_seconds=duration,
        )

        report = await self.reports.generate(query, synthesized, metadata)

        # ── Evidence Graph ────────────────────────────────────────────────────────
        logger.info("EvidenceGraph: construindo grafo de evidências...")
        try:
            from src.evidence_graph import EvidenceGraph

            self.evidence_graph = EvidenceGraph()
            self.evidence_graph.build_from_results(ranked)
            graph_summary = self.evidence_graph.summary()
            if graph_summary:
                report = report + "\n" + graph_summary
                logger.info(
                    f"EvidenceGraph: {len(self.evidence_graph.claims)} claims, "
                    f"{len(self.evidence_graph.relations)} relações"
                )
        except Exception as e:
            logger.warning(f"EvidenceGraph falhou (não crítico): {e}")

        # ── Loop de Auditoria Autônomo ────────────────────────────────────────
        if self.operation_mode.enable_auditor:
            logger.info("ResearchAuditor: Executando loop de auditoria...")
            try:
                audit_result = await self.auditor.audit(
                    report, ranked, max_iterations=2
                )
                report = audit_result.enriched_content
                logger.info(
                    f"ResearchAuditor: Auditoria concluída. {audit_result.audit_summary}"
                )
            except Exception as e:
                logger.error(f"ResearchAuditor: Falha durante auditoria: {e}")

        # ── Peer Review Agent (Executado Primeiro para Calcular Penalidades) ──
        peer_report = None
        peer_md = ""
        if getattr(self.operation_mode, "enable_peer_review", True):
            logger.info(
                "PeerReviewAgent: Executando revisão científica do relatório..."
            )
            try:
                peer_report = await self.peer_reviewer.review(
                    report, ranked, query=query
                )
                peer_md = self.peer_reviewer.to_markdown(peer_report)
                logger.info(
                    f"PeerReviewAgent: {peer_report.critical_count} críticos, "
                    f"{peer_report.major_count} major, {peer_report.minor_count} minor — "
                    f"Parecer: {peer_report.overall_assessment}"
                )
            except Exception as e:
                logger.warning(f"PeerReviewAgent falhou (não crítico): {e}")

        # ── Calculando Research Score ────────────────────────────────────────
        logger.info("Calculando Research Score agregado...")
        try:
            research_score = self.score_aggregator.calculate(
                results=synthesized,
                metadata=metadata,
                all_raw_results=ranked,
                gap_analysis=self._last_gap,
                planned_sources=list(source_plan.primary),
                peer_review_report=peer_report,
            )
            report = self.score_aggregator.inject_into_report(report, research_score)
            logger.info(
                f"Research Score: {research_score.grade} ({research_score.overall:.1%})"
            )
        except Exception as e:
            logger.warning(f"ResearchScoreAggregator falhou (não crítico): {e}")

        # ── Injetando Conflitos ──────────────────────────────────────────────
        conflict_report = getattr(self, "_last_conflict_report", None)
        if conflict_report is not None and conflict_report.conflict_count > 0:
            try:
                conflict_block = self.conflict_detector.format_conflicts_for_report(
                    conflict_report
                )
                if conflict_block:
                    report = report + "\n" + conflict_block
                    logger.info(
                        f"ConflictDetector: {conflict_report.conflict_count} conflitos injetados no relatório"
                    )
            except Exception as e:
                logger.warning(f"Falha ao injetar bloco de conflitos no relatório: {e}")

        # Anexa Peer Review no final se foi gerado
        if peer_md:
            report = report + "\n" + peer_md

        filepath = self.reports.save(report, query, formats=formats)
        logger.info(
            f"Pesquisa completa em {round(duration, 1)}s. Relatorio: {filepath}"
        )

        # Sincroniza Obsidian Vault
        self.reports.sync_to_vault(filepath)

        if synthesized:
            top_entities = [r.title for r in synthesized[:5]]
            exec_summary_snippet = (
                report.split("## 1. Resumo Executivo")[-1].split("---")[0].strip()[:600]
            )
            self.memory_service.store(
                query=query,
                executive_summary=exec_summary_snippet,
                top_entities=top_entities,
                domain=intent.domain.value,
                duration_seconds=duration,
            )

        return report

    # ── Retrocompatibilidade / Delegados do Facade ───────────────────────

    async def _select_scraper_for_url(self, url: str) -> list[Any]:
        """Retrocompatibilidade: delega para `SearchService.select_scraper_for_url`.

        Args:
            url: URL a ser processada pelo scraper adequado.

        Returns:
            list[Any]: Resultados do scraper selecionado para a URL.
        """
        return await self.search.select_scraper_for_url(url)

    async def _parallel_search(self, queries: list[ExpandedQuery], plan, intent):
        """Retrocompatibilidade: delega para `SearchService.execute`.

        Args:
            queries: Queries expandidas a buscar em paralelo.
            plan: Plano de fontes.
            intent: Resultado da analise de intencao.

        Returns:
            list: Resultados brutos de todas as fontes.
        """
        return await self.search.execute(queries, plan, intent)

    async def _search_task(self, searcher, source_name: str, query: str, domain: str):
        """Retrocompatibilidade: delega para `SearchService._search_task`.

        Args:
            searcher: Instancia do searcher a executar.
            source_name: Nome do searcher (para logging e rastreamento).
            query: Query a buscar.
            domain: Dominio da pesquisa.

        Returns:
            list: Resultados brutos do searcher.
        """
        return await self.search._search_task(searcher, source_name, query, domain)

    async def _search_with_timeout(self, searcher, query: str, domain: str):
        """Retrocompatibilidade: delega para `SearchService._search_with_timeout`.

        Args:
            searcher: Instancia do searcher a executar.
            query: Query a buscar.
            domain: Dominio da pesquisa.

        Returns:
            list: Resultados brutos ou lista vazia em timeout.
        """
        return await self.search._search_with_timeout(searcher, query, domain)

    async def _calculate_overall_confidence(self, results: list) -> float:
        """Retrocompatibilidade: delega para `ReasoningService.calculate_overall_confidence`.

        Args:
            results: Lista de resultados com scores de confianca.

        Returns:
            float: Confianca media ponderada (0.0-1.0).
        """
        return await self.reasoning.calculate_overall_confidence(results)

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
