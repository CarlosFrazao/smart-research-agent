import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.config import Config
from src.types import ResearchMetadata, ExpandedQuery
from src.clients.llm_client import LLMClient, LLMProvider
from src.intent_analyzer import IntentAnalyzer
from src.query_expander import QueryExpander
from src.source_planner import SourcePlanner
from src.ranker import QualityRanker
from src.confidence_scorer_v2 import ConfidenceScorerV2
from src.gap_detector import GapDetector
from src.synthesizer import Synthesizer
from src.report_generator import ReportGenerator
from src.search.github_searcher import GitHubSearcher
from src.search.reddit_searcher import RedditSearcher
from src.search.hn_searcher import HNSearcher
from src.search.awesome_searcher import AwesomeSearcher
from src.search.arxiv_searcher import ArxivSearcher
from src.search.producthunt_searcher import ProductHuntSearcher
from src.search.web_searcher import WebSearcher
from src.search.firecrawl_searcher import FirecrawlSearcher
from src.search.spider_searcher import SpiderSearcher
from src.search.steel_searcher import SteelSearcher
from src.search.rss_searcher import RSSSearcher
from src.search.jina_searcher import JinaSearcher
from src.search.searxng_searcher import SearXNGSearcher
from src.search.stackoverflow_searcher import StackOverflowSearcher
from src.search.wayback_searcher import WaybackSearcher
from src.search.semantic_scholar_searcher import SemanticScholarSearcher
from src.search.pubmed_searcher import PubMedSearcher
from src.search.youtube_searcher import YouTubeSearcher
from src.search.playwright_searcher import PlaywrightSearcher
from src.anti_blocking.residential_proxy import ResidentialProxyProvider
from src.clients.smart_model_router import SmartModelRouter, get_router
from src.memory.orvix_memory_v2 import OrvixMemoryV2
from src.confidence_scorer_v2 import ConfidenceScorerV2
from src.link_verifier import LinkVerifier
from src.operation_modes import OperationModes, OperationConfig
from src.research_auditor import ResearchAuditor
from src.monitoring.health_monitor import HealthMonitor
from src.research_score import ResearchScoreAggregator
from src.conflict_detector import ConflictDetector
from src.peer_review_agent import PeerReviewAgent
from src.evidence_graph import EvidenceGraph
from src.utils.cache import Cache
from src.utils.logger import setup_logger
from src.utils.dead_letter_queue import DeadLetterQueue

# Importações dos novos serviços
from src.services.search_service import SearchService
from src.services.reasoning_service import ReasoningService
from src.services.memory_service import MemoryService
from src.services.report_service import ReportService
from src.search.semantic_reranker import SemanticReranker

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
            router = get_router(openrouter_api_key=getattr(self.config, "openrouter_api_key", None))
            logger.info("SmartModelRouter ativo — roteamento de custo habilitado")

        self.llm = LLMClient(
            LLMProvider(self.config.llm_provider),
            llm_config,
            model_router=router,
            fallback_configs=self.config.get_all_llm_configs(),
        )

        self.memory: Optional[OrvixMemoryV2] = None
        if getattr(self.config, "memory_enabled", True):
            try:
                self.memory = OrvixMemoryV2(db_path=getattr(self.config, "memory_db_path", None))
                logger.info(f"OrvixMemoryV2 ativa (RAG Híbrido): {self.memory._db_path}")
            except Exception as e:
                logger.warning(f"OrvixMemoryV2 falhou ao inicializar: {e} — pesquisa continua sem memória")

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
        from src.utils.smart_cache import SmartCache
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
        def fallback_use_ephemeral_chroma(svc, result):
            if self.memory:
                try:
                    import chromadb
                    self.memory.chroma_client = chromadb.Client()
                    self.memory.chroma_collection = self.memory.chroma_client.get_or_create_collection("sra_memories")
                    logger.info("HealthMonitor Fallback: ChromaDB offline. Usando cliente efêmero em memória.")
                except Exception as e:
                    logger.error(f"Erro no fallback do ChromaDB: {e}")

        def fallback_disable_firecrawl(svc, result):
            logger.warning("HealthMonitor Fallback: Desabilitando Firecrawl. Usando Jina/Spider fallback.")
            if "firecrawl" in self.searchers:
                self.searchers["firecrawl"].enabled = False

        def fallback_to_duckduckgo(svc, result):
            logger.warning("HealthMonitor Fallback: SearXNG offline. Priorizando WebSearcher.")
            if "web" in self.searchers:
                self.searchers["web"].enabled = True

        def fallback_disable_cache(svc, result):
            logger.warning("HealthMonitor Fallback: Redis/Cache offline. Desabilitando cache.")
            if hasattr(self, "cache") and self.cache:
                self.cache.enabled = False

        self.health_monitor.register_fallback("use_ephemeral_chroma", fallback_use_ephemeral_chroma)
        self.health_monitor.register_fallback("disable_firecrawl", fallback_disable_firecrawl)
        self.health_monitor.register_fallback("fallback_to_duckduckgo", fallback_to_duckduckgo)
        self.health_monitor.register_fallback("disable_cache", fallback_disable_cache)

    def _init_searchers(self) -> Dict[str, Any]:
        cfg = {
            "timeout": self.config.timeout_per_source,
            "max_results": self.config.max_results_per_source,
            "github_token": self.config.github_token,
            "producthunt_token": self.config.producthunt_token,
            "firecrawl_api_key": self.config.firecrawl_api_key,
            "firecrawl_base_url": self.config.firecrawl_base_url,
            "spider_api_key": self.config.spider_api_key,
            "spider_base_url": self.config.spider_base_url,
            "enabled": True,
            "steel_api_key": self.config.steel_api_key,
            "steel_base_url": self.config.steel_base_url,
        }
        searxng_cfg = {
            **cfg,
            "searxng_url": os.getenv("SEARXNG_URL", "http://127.0.0.1:3023"),
            "searxng_engines": os.getenv("SEARXNG_ENGINES", "google,bing,duckduckgo"),
            "searxng_categories": os.getenv("SEARXNG_CATEGORIES", "general")
        }

        searchers = {
            "github": GitHubSearcher(cfg),
            "reddit": RedditSearcher(cfg),
            "hackernews": HNSearcher(cfg),
            "awesome": AwesomeSearcher(cfg),
            "arxiv": ArxivSearcher(cfg),
            "producthunt": ProductHuntSearcher(cfg),
            "web": WebSearcher(cfg),
            "firecrawl": FirecrawlSearcher(cfg),
            "rss": RSSSearcher({**cfg, "enabled": True}),
            "searxng": SearXNGSearcher(searxng_cfg),
            "stackoverflow": StackOverflowSearcher(cfg),
            "wayback": WaybackSearcher(cfg),
        }
        if self.config.spider_enabled:
            searchers["spider"] = SpiderSearcher(cfg)
        if self.config.steel_enabled:
            searchers["steel"] = SteelSearcher(cfg)
        if getattr(self.config, "host_mode", False):
            logger.info("HOST MODE ativo — Firecrawl substituido por JinaSearcher como fallback")
            jina_cfg = {
                **cfg,
                "jina_base_url": getattr(self.config, "jina_reader_base_url", "https://r.jina.ai/"),
            }
            searchers["firecrawl"] = JinaSearcher(jina_cfg)

        s2_cfg = {
            **cfg,
            "semantic_scholar_api_key": getattr(self.config, "semantic_scholar_api_key", None),
        }
        semantic_scholar = SemanticScholarSearcher(s2_cfg)
        semantic_scholar.web_fallback = searchers.get("web")
        searchers["semantic_scholar"] = semantic_scholar

        pubmed_cfg = {
            **cfg,
            "ncbi_api_key": getattr(self.config, "ncbi_api_key", None),
        }
        pubmed = PubMedSearcher(pubmed_cfg)
        pubmed.web_fallback = searchers.get("web")
        searchers["pubmed"] = pubmed

        youtube_cfg = {
            **cfg,
            "youtube_api_key": getattr(self.config, "youtube_api_key", None),
        }
        youtube = YouTubeSearcher(youtube_cfg)
        youtube.web_fallback = searchers.get("web")
        searchers["youtube"] = youtube

        if getattr(self.config, "playwright_enabled", False):
            proxy_url = None
            if getattr(self.config, "residential_proxy_provider", None):
                try:
                    prov = ResidentialProxyProvider(
                        provider=self.config.residential_proxy_provider,
                        username=self.config.residential_proxy_username or "",
                        password=self.config.residential_proxy_password or "",
                    )
                    proxy_url = prov.get_proxy_url()
                except Exception as e:
                    logger.warning(f"Falha ao configurar proxy residencial: {e}")

            playwright_cfg = {
                **cfg,
                "proxy_url": proxy_url,
                "playwright_headless": getattr(self.config, "playwright_headless", True),
            }
            searchers["playwright"] = PlaywrightSearcher(playwright_cfg)

        # SerpAPI — fallback de último recurso (registro condicional)
        serpapi_key = getattr(self.config, "serpapi_api_key", None)
        serpapi_enabled = getattr(self.config, "serpapi_enabled", True)
        if serpapi_enabled and serpapi_key:
            from src.search.serpapi_searcher import SerpAPISearcher
            searchers["serpapi"] = SerpAPISearcher(api_key=serpapi_key)
            logger.info("SerpAPISearcher registrado como fallback de último recurso")
        else:
            logger.debug("SerpAPISearcher desabilitado (SERPAPI_API_KEY ausente ou serpapi_enabled=false)")

        return searchers

    async def research(self, query: str, formats: Optional[List[Any]] = None) -> str:
        start_time = datetime.now()
        logger.info(f"Iniciando pesquisa: '{query}' [modo: {self.operation_mode.name}]")

        # ── Health Check Inicial ──────────────────────────────────────────────
        try:
            health = await self.health_monitor.check_all()
            if not health.is_healthy:
                logger.warning(f"HealthMonitor: Serviços offline/degradados detectados: {health.alerts}")
        except Exception as e:
            logger.warning(f"Falha ao executar health check: {e}")

        # Intercepta fluxo se o modo for debate
        if getattr(self.operation_mode, "enable_debate", False):
            return await self.reasoning.run_debate_mode(query, start_time, formats=formats)

        memory_context = self.memory_service.get_context(query)

        logger.info("Passo 1/9: Analisando intencao...")
        enriched_query = query
        if memory_context:
            enriched_query = f"{memory_context}\n\n---\n\nQuery atual: {query}"
        intent = await self.reasoning.analyze_intent(enriched_query)
        logger.info(f"  Dominio: {intent.domain.value}, Intencao: {intent.intention.value}")

        logger.info("Passo 2/9: Expandindo queries...")
        expanded_queries = await self.reasoning.expand_queries(query, intent)
        logger.info(f"  {len(expanded_queries)} queries expandidas")

        logger.info("Passo 3/9: Planejando fontes...")
        source_plan = self.source_planner.plan(intent, expanded_queries)
        logger.info(f"  Primarias: {', '.join(source_plan.primary)}")

        logger.info("Passo 4/9: Buscando em paralelo...")
        all_results = await self.search.execute(expanded_queries, source_plan, intent)
        logger.info(f"  {len(all_results)} resultados brutos")

        logger.info("Passo 5/9: Ranqueando e re-ranqueando semanticamente...")
        ranked = await self.reasoning.rank(all_results, query=query)

        logger.info("Passo 5b/9: Scoring de confianca e anti-hallucination...")
        scored = await self.confidence_scorer.score_batch(ranked, cross_validate=True)
        
        logger.info("Passo 5c/9: Verificacao concorrente de links citados...")
        scored = await self.link_verifier.verify_results(scored)
        
        threshold = self.operation_mode.confidence_threshold
        ranked = [r for r in scored if r.confidence_score >= threshold]
        logger.info(f"  {len(ranked)} resultados apos filtro de confianca (>= {threshold})")

        logger.info("Passo 5d/9: Detectando conflitos entre fontes...")
        self._last_conflict_report = None
        try:
            conflict_report = self.conflict_detector.detect(ranked)
            self._last_conflict_report = conflict_report
            if conflict_report.has_critical:
                logger.warning(f"ConflictDetector: {len(conflict_report.critical_conflicts)} conflito(s) crítico(s)")
                conflict_results = await self.conflict_detector.resolve(conflict_report, self)
                if conflict_results:
                    resolved_scored = await self.confidence_scorer.score_batch(conflict_results, cross_validate=True)
                    resolved_scored = await self.link_verifier.verify_results(resolved_scored)
                    resolved_filtered = [r for r in resolved_scored if r.confidence_score >= threshold]
                    
                    ranked.extend(resolved_filtered)
                    ranked.sort(key=lambda x: getattr(x, "score", 0.0) or getattr(x, "confidence_score", 0.0), reverse=True)
                    logger.info(f"ConflictDetector: {len(resolved_filtered)} resultados adicionais inseridos")
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

            logger.info(f"  Gap detectado (iter {iteration + 1}): {gap.missing_aspects}")
            from src.utils.logger import structured_logger
            structured_logger.log_gap(
                gap_description=", ".join(gap.missing_aspects),
                query_used=", ".join(gap.new_queries),
                iteration=iteration + 1
            )
            gap_queries = [
                ExpandedQuery(query=q, type="gap_fill", priority="alta", rationale="gap detection")
                for q in gap.new_queries
            ]
            new_results = await self.search.execute(gap_queries, source_plan, intent)
            new_ranked = await self.reasoning.rank(new_results, query=query)
            ranked.extend(new_ranked)
            ranked.sort(key=lambda x: x.score, reverse=True)
            iteration += 1

        logger.info("Passo 7b/9: Sanitização anti-injection (LLMSanitizer)...")
        sanitized = await self.sanitizer.sanitize_batch([r.description or "" for r in ranked])
        safe_ranked = []
        for r, s in zip(ranked, sanitized):
            if s.risk_score < 0.7:
                r.description = s.cleaned
                safe_ranked.append(r)
            else:
                logger.warning(f"LLMSanitizer bloqueou/filtrou resultado com alto risco ({s.risk_score}) de '{r.url[:50]}'")
        ranked = safe_ranked

        logger.info("Passo 8/9: Sintetizando resultados...")
        synthesized = await self.synthesizer.synthesize(ranked)
        logger.info(f"  {len(synthesized)} entidades sintetizadas")

        logger.info("Passo 9/9: Gerando relatorio...")
        duration = (datetime.now() - start_time).total_seconds()

        metadata = ResearchMetadata(
            query=query,
            domain=intent.domain.value,
            sources=list(set(r.source for r in all_results)) if all_results else list(self.searchers.keys()),
            total_results=len(all_results),
            iterations=iteration + 1,
            timestamp=datetime.now(),
            duration_seconds=duration,
        )

        report = await self.reports.generate(query, synthesized, metadata)

        # ── Evidence Graph ────────────────────────────────────────────────────────
        logger.info("EvidenceGraph: construindo grafo de evidências...")
        try:
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
                audit_result = await self.auditor.audit(report, ranked, max_iterations=2)
                report = audit_result.enriched_content
                logger.info(f"ResearchAuditor: Auditoria concluída. {audit_result.audit_summary}")
            except Exception as e:
                logger.error(f"ResearchAuditor: Falha durante auditoria: {e}")

        # ── Calculando Research Score ────────────────────────────────────────
        logger.info("Calculando Research Score agregado...")
        try:
            research_score = self.score_aggregator.calculate(
                results=synthesized,
                metadata=metadata,
                all_raw_results=ranked,
                gap_analysis=gap,
                planned_sources=list(source_plan.primary),
            )
            report = self.score_aggregator.inject_into_report(report, research_score)
            logger.info(f"Research Score: {research_score.grade} ({research_score.overall:.1%})")
        except Exception as e:
            logger.warning(f"ResearchScoreAggregator falhou (não crítico): {e}")

        # ── Injetando Relatório de Conflitos Detectados ───────────────────────
        if getattr(self, "_last_conflict_report", None) and self._last_conflict_report.conflict_count > 0:
            try:
                conflict_block = self.conflict_detector.format_conflicts_for_report(self._last_conflict_report)
                if conflict_block:
                    report = report + "\n" + conflict_block
                    logger.info(f"ConflictDetector: {self._last_conflict_report.conflict_count} conflitos injetados no relatório")
            except Exception as e:
                logger.warning(f"Falha ao injetar bloco de conflitos no relatório: {e}")

        # ── Peer Review Agent ─────────────────────────────────────────────────
        if getattr(self.operation_mode, "enable_peer_review", True):
            logger.info("PeerReviewAgent: Executando revisão científica do relatório...")
            try:
                peer_report = await self.peer_reviewer.review(report, ranked, query=query)
                peer_md = self.peer_reviewer.to_markdown(peer_report)
                if peer_md:
                    report = report + "\n" + peer_md
                    logger.info(
                        f"PeerReviewAgent: {peer_report.critical_count} críticos, "
                        f"{peer_report.major_count} major, {peer_report.minor_count} minor — "
                        f"Parecer: {peer_report.overall_assessment}"
                    )
            except Exception as e:
                logger.warning(f"PeerReviewAgent falhou (não crítico): {e}")

        filepath = self.reports.save(report, query, formats=formats)
        logger.info(f"Pesquisa completa em {round(duration, 1)}s. Relatorio: {filepath}")

        # Sincroniza Obsidian Vault
        self.reports.sync_to_vault(filepath)

        if synthesized:
            top_entities = [r.title for r in synthesized[:5]]
            exec_summary_snippet = report.split("## 1. Resumo Executivo")[-1].split("---")[0].strip()[:600]
            self.memory_service.store(
                query=query,
                executive_summary=exec_summary_snippet,
                top_entities=top_entities,
                domain=intent.domain.value,
                duration_seconds=duration,
            )

        return report

    # ── Retrocompatibilidade / Delegados do Facade ───────────────────────

    async def _select_scraper_for_url(self, url: str) -> List[Any]:
        return await self.search.select_scraper_for_url(url)

    async def _parallel_search(self, queries: List[ExpandedQuery], plan, intent):
        return await self.search.execute(queries, plan, intent)

    async def _search_task(self, searcher, source_name: str, query: str, domain: str):
        return await self.search._search_task(searcher, source_name, query, domain)

    async def _search_with_timeout(self, searcher, query: str, domain: str):
        return await self.search._search_with_timeout(searcher, query, domain)

    async def _calculate_overall_confidence(self, results: List) -> float:
        return await self.reasoning.calculate_overall_confidence(results)

    # ── Lazy-loaded Service Properties ─────────────────────────────────────

    @property
    def search(self):
        if not hasattr(self, "_search_service") or self._search_service is None:
            from src.services.search_service import SearchService
            self._search_service = SearchService(self)
        return self._search_service

    @search.setter
    def search(self, value):
        self._search_service = value

    @property
    def reasoning(self):
        if not hasattr(self, "_reasoning_service") or self._reasoning_service is None:
            from src.services.reasoning_service import ReasoningService
            self._reasoning_service = ReasoningService(self)
        return self._reasoning_service

    @reasoning.setter
    def reasoning(self, value):
        self._reasoning_service = value

    @property
    def memory_service(self):
        if not hasattr(self, "_memory_service") or self._memory_service is None:
            from src.services.memory_service import MemoryService
            self._memory_service = MemoryService(self)
        return self._memory_service

    @memory_service.setter
    def memory_service(self, value):
        self._memory_service = value

    @property
    def reports(self):
        if not hasattr(self, "_report_service") or self._report_service is None:
            from src.services.report_service import ReportService
            self._report_service = ReportService(self)
        return self._report_service

    @reports.setter
    def reports(self, value):
        self._report_service = value

