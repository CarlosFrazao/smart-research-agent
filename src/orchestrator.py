import asyncio
import logging
import os
import shutil
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.config import Config
from src.types import ResearchMetadata, ExpandedQuery
from src.clients.llm_client import LLMClient, LLMProvider
from src.intent_analyzer import IntentAnalyzer
from src.query_expander import QueryExpander
from src.source_planner import SourcePlanner
from src.ranker import QualityRanker
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

logger = setup_logger("orchestrator")


class Orchestrator:
    def __init__(self, config: Config = None):
        self.config = config or Config()

        llm_config = self.config.get_llm_config()

        router = None
        if getattr(self.config, "smart_routing_enabled", True):
            router = get_router(openrouter_api_key=getattr(self.config, "openrouter_api_key", None))
            logger.info("SmartModelRouter ativo — roteamento de custo habilitado")

        self.llm = LLMClient(LLMProvider(self.config.llm_provider), llm_config, model_router=router)

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

        self.searchers = self._init_searchers()
        self.cache = Cache(cache_dir=self.config.cache_dir)

        # ── Fase 4: Autonomia ─────────────────────────────────────
        # Modo de operação ativo (default: cirurgia)
        mode_name = getattr(self.config, "operation_mode", OperationModes.DEFAULT_MODE)
        self.operation_mode: OperationConfig = OperationModes.get_mode(mode_name)
        logger.info(f"OperationMode ativo: '{self.operation_mode.name}'")

        # ResearchAuditor (loop de auditoria, máx 3 iterações)
        self.auditor = ResearchAuditor(
            llm_client=self.llm,
            orchestrator=self,
            confidence_scorer=self.confidence_scorer,
        )

        # HealthMonitor (verificação de serviços, instanciado sem iniciar o loop)
        self.health_monitor = HealthMonitor()
        self.health_monitor.orchestrator = self

        # Registro de fallbacks para o HealthMonitor
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

        logger.info("Orchestrator inicializado")

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
        # Adiciona chaves de ambiente customizadas para o SearXNG
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

        # Semantic Scholar — sempre ativo, sem dependências externas críticas
        s2_cfg = {
            **cfg,
            "semantic_scholar_api_key": getattr(self.config, "semantic_scholar_api_key", None),
        }
        semantic_scholar = SemanticScholarSearcher(s2_cfg)
        # Injeta web fallback após criação para evitar dependência circular
        semantic_scholar.web_fallback = searchers.get("web")
        searchers["semantic_scholar"] = semantic_scholar

        # PubMed
        pubmed_cfg = {
            **cfg,
            "ncbi_api_key": getattr(self.config, "ncbi_api_key", None),
        }
        pubmed = PubMedSearcher(pubmed_cfg)
        pubmed.web_fallback = searchers.get("web")
        searchers["pubmed"] = pubmed

        # YouTube
        youtube_cfg = {
            **cfg,
            "youtube_api_key": getattr(self.config, "youtube_api_key", None),
        }
        youtube = YouTubeSearcher(youtube_cfg)
        youtube.web_fallback = searchers.get("web")
        searchers["youtube"] = youtube

        return searchers

    async def _select_scraper_for_url(self, url: str) -> List[Any]:
        """
        Smart cascade: tries scrapers in priority order for a given URL.

        Priority:
        1. Firecrawl — default, reliable, clean markdown
        2. Spider.cloud — if Firecrawl times out (>10s), raises an error or returns empty
        3. Steel.dev — if Spider returns empty content (JS-heavy page suspected)
        4. Jina Reader — zero-setup final fallback (prefixes URL with r.jina.ai/)
        """
        firecrawl = self.searchers.get("firecrawl")
        spider = self.searchers.get("spider")
        steel = self.searchers.get("steel")

        # 1. Tentativa primária: Firecrawl
        if firecrawl:
            try:
                result = await asyncio.wait_for(firecrawl.search(url), timeout=10.0)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
                logger.warning(f"Firecrawl content too short/empty for '{url[:50]}'")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Firecrawl failed for '{url[:50]}': {e}")

        # 2. Tentativa secundária: Spider (se habilitado)
        if spider and self.config.spider_enabled:
            try:
                result = await spider.search(url)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
            except Exception as e:
                logger.warning(f"Spider failed for '{url[:50]}': {e}")

        # 3. Tentativa terciária: Steel (se habilitado)
        if steel and self.config.steel_enabled:
            try:
                result = await steel.search(url)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
            except Exception as e:
                logger.warning(f"Steel failed for '{url[:50]}': {e}")

        # 4. Fallback incondicional final: Jina Reader
        jina_url = f"https://r.jina.ai/{url}"
        try:
            logger.info(f"Using Jina Reader fallback for '{url[:50]}'")
            raw = await self.searchers["firecrawl"].client.scrape(jina_url)
            if raw and raw.get("markdown"):
                from src.types import SearchResult
                return [SearchResult(
                    source="jina_reader",
                    title=f"Jina: {url}",
                    url=url,
                    description=str(raw.get("markdown", "")),
                    metrics={},
                    raw=raw,
                )]
        except Exception as e:
            logger.warning(f"Jina Reader failed for '{url[:50]}': {e}")

        # Fallback de desespero: se tudo falhar, retorna o primeiro resultado que o Firecrawl obteve
        if firecrawl:
            try:
                result = await firecrawl.search(url)
                if result:
                    return result
            except Exception:
                pass

        return []

    async def research(self, query: str) -> str:
        start_time = datetime.now()
        logger.info(f"Iniciando pesquisa: '{query}' [modo: {self.operation_mode.name}]")

        # ── Health Check Inicial ──────────────────────────────────────────────
        try:
            health = await self.health_monitor.check_all()
            if not health.is_healthy:
                logger.warning(f"HealthMonitor: Serviços offline/degradados detectados: {health.alerts}")
        except Exception as e:
            logger.warning(f"Falha ao executar health check: {e}")

        # Intercepta fluxo se o modo for debate (Bloco 3.1)
        if getattr(self.operation_mode, "enable_debate", False):
            logger.info("Modo DEBATE ativo. Iniciando DebateOrchestrator...")
            from src.debate_orchestrator import DebateOrchestrator
            debate = DebateOrchestrator(llm_client=self.llm, searchers=self.searchers)
            debate_round = await debate.run(query)
            report = debate.format_debate_markdown(debate_round)
            
            # Salvar e sincronizar
            duration = (datetime.now() - start_time).total_seconds()
            filepath = self.report_generator.save_report(report, query, self.config.output_dir)
            logger.info(f"Debate completo em {round(duration, 1)}s. Relatorio: {filepath}")

            if getattr(self.config, "obsidian_vault_path", None) and getattr(self.config, "obsidian_auto_sync", False):
                try:
                    vault_dir = self.config.obsidian_vault_path
                    os.makedirs(vault_dir, exist_ok=True)
                    vault_path = os.path.join(vault_dir, os.path.basename(filepath))
                    shutil.copy2(filepath, vault_path)
                    logger.info(f"Obsidian sync: {vault_path}")
                except Exception as e:
                    logger.warning(f"Obsidian sync falhou (nao critico): {e}")

            if self.memory:
                try:
                    self.memory.store_research_result(
                        query=query,
                        executive_summary=f"Debate vencedor: {debate_round.winner}. Veredito: {debate_round.verdict}",
                        top_entities=[debate_round.winner] if debate_round.winner else [],
                        domain="general",
                        duration_seconds=duration,
                    )
                except Exception as e:
                    logger.warning(f"OrvixMemory.store_research_result falhou para o debate: {e}")

            return report

        memory_context = ""
        if self.memory:
            try:
                memory_context = self.memory.get_context(query, top_k=3)
                if memory_context:
                    logger.info("OrvixMemory: contexto de pesquisas anteriores recuperado")
            except Exception as e:
                logger.warning(f"OrvixMemory.get_context falhou: {e}")

        logger.info("Passo 1/9: Analisando intencao...")
        enriched_query = query
        if memory_context:
            enriched_query = f"{memory_context}\n\n---\n\nQuery atual: {query}"
        intent = await self.intent_analyzer.analyze(enriched_query)
        logger.info(f"  Dominio: {intent.domain.value}, Intencao: {intent.intention.value}")

        logger.info("Passo 2/9: Expandindo queries...")
        expanded_queries = await self.query_expander.expand(query, intent)
        logger.info(f"  {len(expanded_queries)} queries expandidas")

        logger.info("Passo 3/9: Planejando fontes...")
        source_plan = self.source_planner.plan(intent, expanded_queries)
        logger.info(f"  Primarias: {', '.join(source_plan.primary)}")

        logger.info("Passo 4/9: Buscando em paralelo...")
        all_results = await self._parallel_search(expanded_queries, source_plan, intent)
        logger.info(f"  {len(all_results)} resultados brutos")

        logger.info("Passo 5/9: Ranqueando resultados...")
        ranked = await self.ranker.rank(all_results)

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
            new_results = await self._parallel_search(gap_queries, source_plan, intent)
            new_ranked = await self.ranker.rank(new_results)
            ranked.extend(new_ranked)
            ranked.sort(key=lambda x: x.score, reverse=True)
            iteration += 1

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

        report = await self.report_generator.generate(query, synthesized, metadata)

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

        filepath = self.report_generator.save_report(report, query, self.config.output_dir)
        logger.info(f"Pesquisa completa em {round(duration, 1)}s. Relatorio: {filepath}")

        if getattr(self.config, "obsidian_vault_path", None) and getattr(self.config, "obsidian_auto_sync", False):
            try:
                vault_dir = self.config.obsidian_vault_path
                os.makedirs(vault_dir, exist_ok=True)
                vault_path = os.path.join(vault_dir, os.path.basename(filepath))
                shutil.copy2(filepath, vault_path)
                logger.info(f"Obsidian sync: {vault_path}")
            except Exception as e:
                logger.warning(f"Obsidian sync falhou (nao critico): {e}")

        if self.memory and synthesized:
            try:
                top_entities = [r.title for r in synthesized[:5]]
                exec_summary_snippet = report.split("## 1. Resumo Executivo")[-1].split("---")[0].strip()[:600]
                self.memory.store_research_result(
                    query=query,
                    executive_summary=exec_summary_snippet,
                    top_entities=top_entities,
                    domain=intent.domain.value,
                    duration_seconds=duration,
                )
            except Exception as e:
                logger.warning(f"OrvixMemory.store_research_result falhou: {e}")

        return report

    async def _search_task(self, searcher, source_name: str, query: str, domain: str):
        from src.utils.logger import structured_logger
        error_msg = None
        res = []
        try:
            res = await self._search_with_timeout(searcher, query, domain)
        except Exception as e:
            error_msg = str(e)
        structured_logger.log_search(source_name, query, len(res), error_msg)
        return source_name, query, res

    async def _parallel_search(self, queries: List[ExpandedQuery], plan, intent):
        from src.types import SearchResult
        tasks = []
        results = []

        # Injeta RSSSearcher se for query urgente/recente de tecnologia e o RSS estiver habilitado
        if intent.urgency == "sim" and intent.domain.value in ("ai_ml", "dev_tools", "saas_b2b") and queries:
            rss = self.searchers.get("rss")
            if rss and rss.enabled:
                primary_query = queries[0].query
                cache_key = f"rss:{primary_query}"
                cached = self.cache.get("search", cache_key)
                if cached is not None:
                    logger.debug(f"Cache hit para RSS: {cache_key}")
                    deserialized = []
                    for r in cached:
                        if "fetched_at" in r and isinstance(r["fetched_at"], str):
                            try:
                                r["fetched_at"] = datetime.fromisoformat(r["fetched_at"])
                            except Exception:
                                r["fetched_at"] = datetime.now()
                        deserialized.append(SearchResult(**r))
                    results.extend(deserialized)
                else:
                    task = asyncio.create_task(
                        self._search_task(rss, "rss", primary_query, intent.domain.value),
                        name=f"rss:{primary_query[:30]}",
                    )
                    tasks.append(task)

        for source_name, source_queries in plan.sources.items():
            if source_name not in self.operation_mode.searchers:
                logger.debug(f"Searcher '{source_name}' filtrado (desabilitado no modo '{self.operation_mode.name}')")
                continue
            searcher = self.searchers.get(source_name)
            if not searcher or not searcher.enabled:
                continue
            for eq in source_queries:
                from src.query_validator import QueryValidator
                sanitized = QueryValidator.sanitize(eq.query)
                if not QueryValidator.is_valid(sanitized):
                    logger.warning(f"Query desconsiderada por ser inválida ou malformada: '{eq.query[:50]}'")
                    continue
                eq.query = sanitized
                cache_key = f"{source_name}:{eq.query}"
                cached = self.cache.get("search", cache_key)
                if cached is not None:
                    logger.debug(f"Cache hit: {cache_key}")
                    deserialized = []
                    for r in cached:
                        if "fetched_at" in r and isinstance(r["fetched_at"], str):
                            try:
                                r["fetched_at"] = datetime.fromisoformat(r["fetched_at"])
                            except Exception:
                                r["fetched_at"] = datetime.now()
                        deserialized.append(SearchResult(**r))
                    results.extend(deserialized)
                    continue

                task = asyncio.create_task(
                    self._search_task(searcher, source_name, eq.query, intent.domain.value),
                    name=f"{source_name}:{eq.query[:30]}",
                )
                tasks.append(task)

        for task in asyncio.as_completed(tasks):
            try:
                source_name, query_str, res = await task
                results.extend(res)
                if res:
                    self.cache.set(
                        "search",
                        f"{source_name}:{query_str}",
                        [r.__dict__ for r in res],
                    )
            except Exception as e:
                logger.warning(f"Busca falhou: {e}")

        return results

    async def _search_with_timeout(self, searcher, query: str, domain: str):
        try:
            return await asyncio.wait_for(
                searcher.search(query, domain=domain),
                timeout=searcher.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout em {searcher.__class__.__name__}")
            if self.health_monitor:
                cls_name = searcher.__class__.__name__.lower()
                source_name = "hackernews" if "hn" in cls_name else cls_name.replace("searcher", "")
                self.health_monitor.report_failure(source_name, "TimeoutError")
            return searcher.fallback(query)
        except Exception as e:
            logger.error(f"Erro em {searcher.__class__.__name__}: {e}")
            if self.health_monitor:
                cls_name = searcher.__class__.__name__.lower()
                source_name = "hackernews" if "hn" in cls_name else cls_name.replace("searcher", "")
                self.health_monitor.report_failure(source_name, str(e))
            return []

