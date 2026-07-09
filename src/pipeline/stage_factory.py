"""Stage Factory — Factory para criação de stages do pipeline com DI e lazy init.

Centraliza a criação, configuração e cache de instâncias de PipelineStage,
eliminando a necessidade de cada consumidor conhecer as dependências
específicas de cada stage.

Funcionalidades:
  - `create_stage(name, config)`: Cria stage com DI automático
  - Lazy initialization: Só instancia quando primeiro usado
  - Cache de instâncias: Reusa stages já criados (singleton por config)
  - Registro customizado: Permite registrar factories próprias
  - Override para testes: Substitui dependências em ambiente de teste
  - Ciclo de vida: Suporte a init/shutdown de stages stateful
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.pipeline.pipeline import PipelineStage, ResearchPipeline

logger = logging.getLogger("pipeline.stage_factory")


# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_STAGE_NAMES: List[str] = [
    "intent",
    "storm",
    "expand",
    "search",
    "rank",
    "score",
    "graph_explorer",
    "gap",
    "synthesize",
    "report",
    "audit",
]


# ── Configuração ─────────────────────────────────────────────────────────────


@dataclass
class StageFactoryConfig:
    """Configuração da factory de stages."""

    # Cache
    enable_cache: bool = True
    cache_key_strategy: str = "name_and_config_hash"  # ou "name_only"

    # Lazy init
    enable_lazy_init: bool = True

    # Test overrides
    test_mode: bool = False

    # Ciclo de vida
    auto_shutdown_on_exit: bool = True

    # Logging
    log_creation: bool = True


# ── StageFactory ─────────────────────────────────────────────────────────────


class StageFactory:
    """Factory centralizada para criação de PipelineStage com DI.

    Responsabilidades:
      1. Resolver dependências de cada stage automaticamente
      2. Cachear instâncias (evita recriar stages stateful)
      3. Suportar lazy initialization
      4. Permitir override de factories (útil para testes)
      5. Gerenciar ciclo de vida (init/shutdown)
    """

    @staticmethod
    def initialize_components(orchestrator: Any, config: Any) -> None:
        """Inicializa e vincula todos os componentes clássicos ao orquestrador (Wiring)."""
        llm_config = config.get_llm_config()

        from src.clients.smart_model_router import get_router

        router = None
        if getattr(config, "smart_routing_enabled", True):
            try:
                router = get_router(
                    openrouter_api_key=getattr(config, "openrouter_api_key", None)
                )
            except Exception:
                pass

        from src.clients.llm_client import LLMClient, LLMProvider

        orchestrator.llm = LLMClient(
            LLMProvider(config.llm_provider),
            llm_config,
            model_router=router,
            fallback_configs=config.get_all_llm_configs(),
        )

        from src.memory.orvix_memory import OrvixMemoryV2

        orchestrator.memory = None
        if getattr(config, "memory_enabled", True):
            try:
                orchestrator.memory = OrvixMemoryV2(
                    db_path=getattr(config, "memory_db_path", None)
                )
            except Exception:
                pass

        from src.memory.knowledge_graph import KnowledgeGraph

        orchestrator.knowledge_graph = KnowledgeGraph(config)

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
        from src.evidence_graph import EvidenceGraph
        from src.search.semantic_reranker import SemanticReranker
        from src.security.llm_sanitizer import LLMSanitizer
        from src.cache import Cache
        from src.utils.dead_letter_queue import DeadLetterQueue
        from src.monitoring.health_monitor import HealthMonitor
        from src.operation_modes import OperationModes
        from src.research_auditor import ResearchAuditor

        orchestrator.intent_analyzer = IntentAnalyzer(orchestrator.llm)
        orchestrator.query_expander = QueryExpander(orchestrator.llm)
        orchestrator.source_planner = SourcePlanner(llm=orchestrator.llm)
        orchestrator.ranker = QualityRanker(orchestrator.llm)
        orchestrator.confidence_scorer = ConfidenceScorerV2(llm_client=orchestrator.llm)
        orchestrator.gap_detector = GapDetector(orchestrator.llm)
        orchestrator.synthesizer = Synthesizer(orchestrator.llm)
        orchestrator.report_generator = ReportGenerator(orchestrator.llm)
        orchestrator.link_verifier = LinkVerifier()
        orchestrator.score_aggregator = ResearchScoreAggregator()
        orchestrator.conflict_detector = ConflictDetector(llm_client=orchestrator.llm)
        orchestrator.peer_reviewer = PeerReviewAgent(llm_client=orchestrator.llm)
        orchestrator.evidence_graph = EvidenceGraph()
        orchestrator.semantic_reranker = SemanticReranker()
        orchestrator.sanitizer = LLMSanitizer(orchestrator.llm)

        from src.search.factory import SearcherFactory

        orchestrator.searchers = SearcherFactory.create_searchers(orchestrator)
        orchestrator.cache = Cache(cache_dir=config.cache_dir)

        # SmartCache
        orchestrator.smart_cache = Cache(redis_url=getattr(config, "redis_url", None))
        orchestrator.dlq = DeadLetterQueue(path=getattr(config, "dlq_path", "./.dlq"))

        # OperationMode
        mode_name = getattr(config, "operation_mode", OperationModes.DEFAULT_MODE)
        orchestrator.operation_mode = OperationModes.get_mode(mode_name)

        # ResearchAuditor
        orchestrator.auditor = ResearchAuditor(
            llm_client=orchestrator.llm,
            confidence_scorer=orchestrator.confidence_scorer,
        )

        # HealthMonitor
        orchestrator.health_monitor = HealthMonitor()
        orchestrator.health_monitor.orchestrator = orchestrator

        # Instanciação dos novos Serviços (Facade)
        from src.services.search_service import SearchService
        from src.services.reasoning_service import ReasoningService
        from src.services.memory_service import MemoryService
        from src.services.report_service import ReportService

        orchestrator._search_service = SearchService(orchestrator)
        orchestrator._reasoning_service = ReasoningService(orchestrator)
        orchestrator._memory_service = MemoryService(orchestrator)
        orchestrator._report_service = ReportService(orchestrator)

    @staticmethod
    def build_pipeline(orchestrator: Any) -> ResearchPipeline:
        """Cria e retorna o ResearchPipeline padrão para o orquestrador."""
        factory = StageFactory(
            orchestrator=orchestrator,
            llm_client=orchestrator.llm,
            cache=orchestrator.cache,
            config=orchestrator.config,
        )
        stage_names = [
            "intent",
            "storm",
            "expand",
            "search",
            "rank",
            "score",
            "verification",  # ← NOVA LINHA
            "graph_explorer",
            "gap",
            "synthesize",
            "report",
        ]
        return factory.create_pipeline(stage_names)

    def __init__(
        self,
        orchestrator: Optional[Any] = None,
        llm_client: Optional[Any] = None,
        cache: Optional[Any] = None,
        config: Optional[Any] = None,
        metrics_collector: Optional[Any] = None,
        circuit_breaker_registry: Optional[Any] = None,
        embedding_model: Optional[Any] = None,
        factory_config: Optional[StageFactoryConfig] = None,
    ):
        # Dependências compartilhadas (injetadas em todos os stages)
        self._deps = {
            "orchestrator": orchestrator,
            "llm_client": llm_client,
            "llm": llm_client,  # alias
            "cache": cache,
            "config": config,
            "metrics_collector": metrics_collector,
            "metrics": metrics_collector,  # alias
            "circuit_breaker_registry": circuit_breaker_registry,
            "cb_registry": circuit_breaker_registry,  # alias
            "embedding_model": embedding_model,
        }

        self.factory_config = factory_config or StageFactoryConfig()

        # Registro de factories customizadas
        self._registry: Dict[str, Callable[[], PipelineStage]] = {}
        self._overrides: Dict[str, Callable[[], PipelineStage]] = {}

        # Cache de instâncias
        self._cache: Dict[str, PipelineStage] = {}
        self._cache_lock = threading.Lock()

        # Tracking de inicializações pendentes (lazy)
        self._lazy_registry: Dict[str, Callable[[], PipelineStage]] = {}

        # Stages que precisam de shutdown
        self._stateful_stages: List[PipelineStage] = []

        # Registra factories built-in
        self._register_builtin_factories()

        logger.info("StageFactory inicializada")

    # ── API Pública ─────────────────────────────────────────────────────────

    def create_stage(
        self,
        stage_name: str,
        stage_config: Optional[Dict[str, Any]] = None,
        use_cache: Optional[bool] = None,
    ) -> PipelineStage:
        """Cria um stage com DI automático.

        Args:
            stage_name: Nome do stage (ex: 'search', 'intent', 'rank').
            stage_config: Configuração específica do stage (merge com global).
            use_cache: Se True, reusa instância cacheada. Se None, usa factory_config.

        Returns:
            PipelineStage: Instância pronta para uso.

        Raises:
            StageFactoryError: Se o stage não está registrado.
        """
        use_cache = (
            use_cache if use_cache is not None else self.factory_config.enable_cache
        )
        cache_key = self._build_cache_key(stage_name, stage_config)

        # 1. Verifica cache
        if use_cache:
            with self._cache_lock:
                if cache_key in self._cache:
                    if self.factory_config.log_creation:
                        logger.debug(f"StageFactory: cache hit para '{stage_name}'")
                    return self._cache[cache_key]

        # 2. Verifica overrides (testes)
        if stage_name in self._overrides:
            stage = self._overrides[stage_name]()
            if self.factory_config.log_creation:
                logger.info(f"StageFactory: stage '{stage_name}' criado via override")
            self._store_in_cache(cache_key, stage, use_cache)
            return stage

        # 3. Verifica registro customizado
        if stage_name in self._registry:
            stage = self._registry[stage_name]()
            if self.factory_config.log_creation:
                logger.info(f"StageFactory: stage '{stage_name}' criado via registry")
            self._store_in_cache(cache_key, stage, use_cache)
            return stage

        # 4. Lazy initialization: registra para criar depois
        if self.factory_config.enable_lazy_init and stage_name in self._lazy_registry:
            factory_fn = self._lazy_registry[stage_name]
            stage = factory_fn()
            if self.factory_config.log_creation:
                logger.info(f"StageFactory: stage '{stage_name}' criado via lazy init")
            self._store_in_cache(cache_key, stage, use_cache)
            return stage

        raise StageFactoryError(
            f"Stage '{stage_name}' não registrado. "
            f"Stages disponíveis: {self.get_available_stages()}"
        )

    def create_pipeline(
        self,
        stage_names: List[str],
        pipeline_config: Optional[Dict[str, Any]] = None,
        stop_on_error: bool = True,
    ) -> ResearchPipeline:
        """Cria um ResearchPipeline com stages na ordem especificada.

        Args:
            stage_names: Lista de nomes de stages (ex: ['intent', 'search', 'report']).
            pipeline_config: Configuração por stage {stage_name: config_dict}.
            stop_on_error: Se True, para pipeline em caso de falha (mantido para compatibilidade).

        Returns:
            ResearchPipeline: Pipeline configurado e pronto para executar.
        """
        stages: List[PipelineStage] = []
        pipeline_config = pipeline_config or {}

        for name in stage_names:
            stage_cfg = pipeline_config.get(name)
            stage = self.create_stage(name, stage_cfg)
            stages.append(stage)

        pipeline = ResearchPipeline(stages)
        logger.info(f"StageFactory: pipeline criado com {len(stages)} stages")
        return pipeline

    def register(
        self,
        stage_name: str,
        factory: Callable[[], PipelineStage],
        lazy: bool = False,
    ) -> None:
        """Registra uma factory customizada para um stage.

        Args:
            stage_name: Nome identificador do stage.
            factory: Função que retorna uma instância de PipelineStage.
            lazy: Se True, só executa factory na primeira chamada.
        """
        if lazy:
            self._lazy_registry[stage_name] = factory
            logger.debug(f"StageFactory: factory lazy registrada para '{stage_name}'")
        else:
            self._registry[stage_name] = factory
            logger.debug(f"StageFactory: factory registrada para '{stage_name}'")

    def override(self, stage_name: str, factory: Callable[[], PipelineStage]) -> None:
        """Override de factory (útil para testes/mocking).

        Tem prioridade sobre registry e lazy registry.
        """
        self._overrides[stage_name] = factory
        # Limpa cache para este stage
        self._invalidate_cache(stage_name)
        logger.info(f"StageFactory: override registrado para '{stage_name}'")

    def remove_override(self, stage_name: str) -> None:
        """Remove override de um stage."""
        self._overrides.pop(stage_name, None)
        self._invalidate_cache(stage_name)
        logger.debug(f"StageFactory: override removido para '{stage_name}'")

    def clear_cache(self) -> None:
        """Limpa cache de instâncias."""
        with self._cache_lock:
            self._cache.clear()
        logger.info("StageFactory: cache limpo")

    def get_cached_stages(self) -> List[str]:
        """Retorna lista de stages atualmente em cache."""
        with self._cache_lock:
            return list(self._cache.keys())

    def get_available_stages(self) -> List[str]:
        """Retorna lista de todos os stages registrados."""
        names = set(self._registry.keys())
        names.update(self._lazy_registry.keys())
        names.update(self._overrides.keys())
        return sorted(names)

    async def shutdown(self) -> None:
        """Executa shutdown de todos os stages stateful.

        Chama rollback() ou close() em stages que implementam.
        """
        for stage in self._stateful_stages:
            try:
                if hasattr(stage, "close") and callable(stage.close):
                    if asyncio.iscoroutinefunction(stage.close):
                        await stage.close()
                    else:
                        stage.close()
                elif hasattr(stage, "rollback") and callable(stage.rollback):
                    if asyncio.iscoroutinefunction(stage.rollback):
                        await stage.rollback(None)  # type: ignore
            except Exception as e:
                logger.warning(f"Erro no shutdown de '{stage.name}': {e}")

        self._stateful_stages.clear()
        self.clear_cache()
        logger.info("StageFactory: shutdown completo")

    # ── Factories Built-in ────────────────────────────────────────────────────

    def _register_builtin_factories(self) -> None:
        """Registra factories para todos os stages built-in do SRA.

        Cada factory resolve dependências do `_deps` e cria o stage.
        """
        # Intent Stage
        self.register("intent", self._create_intent_stage, lazy=True)
        self.register("intent_analysis", self._create_intent_stage, lazy=True)

        # Storm Stage (perspectivas multi-especialista, antes do Expand)
        self.register("storm", self._create_storm_stage, lazy=True)
        self.register("storm_perspectives", self._create_storm_stage, lazy=True)

        # Expand Stage
        self.register("expand", self._create_expand_stage, lazy=True)
        self.register("query_expansion", self._create_expand_stage, lazy=True)

        # Search Stage
        self.register("search", self._create_search_stage, lazy=True)

        # Rank Stage
        self.register("rank", self._create_rank_stage, lazy=True)
        self.register("ranking", self._create_rank_stage, lazy=True)

        # Score Stage
        self.register("score", self._create_score_stage, lazy=True)
        self.register("scoring", self._create_score_stage, lazy=True)

        # Graph Explorer Stage (análise de densidade do Grafo de Conhecimento)
        self.register("graph_explorer", self._create_graph_explorer_stage, lazy=True)
        self.register("graph_gap", self._create_graph_explorer_stage, lazy=True)

        # Gap Stage
        self.register("gap", self._create_gap_stage, lazy=True)
        self.register("gap_detection", self._create_gap_stage, lazy=True)

        # Synthesize Stage
        self.register("synthesize", self._create_synthesize_stage, lazy=True)
        self.register("synthesis", self._create_synthesize_stage, lazy=True)

        # Report Stage
        self.register("report", self._create_report_stage, lazy=True)
        self.register("report_generation", self._create_report_stage, lazy=True)

        # Audit Stage
        self.register("audit", self._create_audit_stage, lazy=True)

        # Verification Stage (Fase 1A — sandbox de código)
        self.register("verification", self._create_verification_stage, lazy=True)

    def _create_intent_stage(self) -> PipelineStage:
        """Factory para IntentStage."""
        from src.pipeline.stages.intent_stage import IntentStage
        from src.intent_analyzer import IntentAnalyzer

        analyzer = IntentAnalyzer(llm_client=self._deps.get("llm_client"))
        return IntentStage(intent_analyzer=analyzer)

    def _create_storm_stage(self) -> PipelineStage:
        """Factory para StormStage (perspectivas multi-especialista STORM)."""
        from src.pipeline.stages.storm_stage import StormStage

        config = self._deps.get("config")
        enabled = getattr(config, "storm_enabled", True) if config else True
        num_perspectives = getattr(config, "storm_num_perspectives", 3) if config else 3

        return StormStage(
            llm_client=self._deps.get("llm_client"),
            cache=self._deps.get("cache"),
            num_perspectives=num_perspectives,
            enabled=enabled,
        )

    def _create_expand_stage(self) -> PipelineStage:
        """Factory para ExpandStage."""
        from src.pipeline.stages.expand_stage import ExpandStage
        from src.query_expander import QueryExpander

        expander = QueryExpander(llm_client=self._deps.get("llm_client"))
        return ExpandStage(query_expander=expander, cache=self._deps.get("cache"))

    def _create_search_stage(self) -> PipelineStage:
        """Factory para SearchStage."""
        from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig
        from src.utils.circuit_breaker import CircuitBreakerRegistry

        searchers = {}
        orch = self._deps.get("orchestrator")
        if orch and hasattr(orch, "searchers"):
            searchers = orch.searchers

        cb_registry = self._deps.get("circuit_breaker_registry")
        if cb_registry is None:
            cb_registry = CircuitBreakerRegistry()

        return SearchStage(
            searchers=searchers,
            cache=self._deps.get("cache"),
            ranker=getattr(orch, "ranker", None) if orch else None,
            config=SearchStageConfig(),
            circuit_breaker_registry=cb_registry,
            health_monitor=getattr(orch, "health_monitor", None) if orch else None,
            sanitizer=getattr(orch, "sanitizer", None) if orch else None,
        )

    def _create_rank_stage(self) -> PipelineStage:
        """Factory para RankStage (ranking híbrido)."""
        from src.pipeline.stages.rank_stage import RankStage
        from src.search.semantic_reranker import SemanticReranker
        from src.feedback_store import FeedbackStore

        embedding_ranker = SemanticReranker()
        feedback_store = self._deps.get("feedback_store") or FeedbackStore()

        return RankStage(
            embedding_ranker=embedding_ranker,
            llm_reranker=self._deps.get("llm_client"),
            feedback_store=feedback_store,
        )

    def _create_score_stage(self) -> PipelineStage:
        """Factory para ScoreStage."""
        from src.pipeline.stages.score_stage import ScoreStage

        return ScoreStage(llm_client=self._deps.get("llm_client"))

    def _create_graph_explorer_stage(self) -> PipelineStage:
        """Factory para GraphExplorerStage.

        Reaproveita `orchestrator.memory.kg` — instância real de
        `SemanticKnowledgeGraph` já criada dentro de `OrvixMemory.__init__`
        (ver `src/memory/orvix_memory.py`) — em vez de abrir uma segunda
        conexão KuzuDB. Se `orchestrator` não estiver disponível nos deps
        (uso standalone/testes) ou `memory`/`memory.kg` não existir, o
        agente é criado com `knowledge_graph=None` e o próprio
        `GraphExplorerAgent` sabe devolver um relatório vazio em vez de
        falhar (ver docstring de `src/graph_explorer_agent.py`).
        """
        from src.graph_explorer_agent import GraphExplorerAgent
        from src.pipeline.stages.graph_explorer_stage import GraphExplorerStage

        orch = self._deps.get("orchestrator")
        kg = None
        if orch is not None:
            memory = getattr(orch, "memory", None)
            kg = getattr(memory, "kg", None) if memory is not None else None

        agent = GraphExplorerAgent(knowledge_graph=kg, llm=self._deps.get("llm_client"))
        return GraphExplorerStage(graph_explorer_agent=agent)

    def _create_gap_stage(self) -> PipelineStage:
        """Factory para GapFillStage."""
        from src.pipeline.stages import GapFillStage

        return GapFillStage()

    def _create_synthesize_stage(self) -> PipelineStage:
        """Factory para SynthesizeStage."""
        from src.pipeline.stages.synthesize_stage import SynthesizeStage
        from src.synthesizer import Synthesizer

        synthesizer = Synthesizer(llm_client=self._deps.get("llm_client"))
        return SynthesizeStage(synthesizer=synthesizer)

    def _create_report_stage(self) -> PipelineStage:
        """Factory para ReportStage."""
        from src.pipeline.stages.report_stage import ReportStage

        return ReportStage(
            llm_client=self._deps.get("llm_client"), cache=self._deps.get("cache")
        )

    def _create_audit_stage(self) -> PipelineStage:
        """Factory para SanitizationStage."""
        from src.pipeline.stages import SanitizationStage

        return SanitizationStage()

    def _create_verification_stage(self) -> PipelineStage:
        """Factory para VerificationStage (sandbox Docker de código)."""
        from src.pipeline.stages.verification_stage import VerificationStage
        from src.services.code_execution_agent import CodeExecutionAgent

        code_agent = self._deps.get("code_execution_agent")
        if code_agent is None:
            code_agent = CodeExecutionAgent()
            self._deps["code_execution_agent"] = code_agent

        return VerificationStage(
            code_agent=code_agent,
            llm_client=self._deps.get("llm_client"),
        )

    # ── Helpers internos ────────────────────────────────────────────────────

    def _build_cache_key(
        self, stage_name: str, stage_config: Optional[Dict[str, Any]]
    ) -> str:
        """Constrói chave de cache determinística."""
        if self.factory_config.cache_key_strategy == "name_only":
            return stage_name

        if stage_config:
            import hashlib

            config_str = str(sorted(stage_config.items()))
            config_hash = hashlib.md5(config_str.encode()).hexdigest()[:8]
            return f"{stage_name}:{config_hash}"
        return stage_name

    def _store_in_cache(
        self,
        cache_key: str,
        stage: PipelineStage,
        use_cache: bool,
    ) -> None:
        """Armazena stage no cache se configurado."""
        if not use_cache:
            return

        with self._cache_lock:
            self._cache[cache_key] = stage

        # Tracking de stages stateful
        if hasattr(stage, "close") or hasattr(stage, "rollback"):
            self._stateful_stages.append(stage)

    def _invalidate_cache(self, stage_name: str) -> None:
        """Remove do cache entradas que começam com stage_name."""
        with self._cache_lock:
            keys_to_remove = [k for k in self._cache if k.startswith(stage_name)]
            for k in keys_to_remove:
                del self._cache[k]


# ── Exceções ───────────────────────────────────────────────────────────────


class StageFactoryError(Exception):
    """Erro na criação de stage pela factory."""

    pass


class StageNotFoundError(StageFactoryError):
    """Stage solicitado não está registrado."""

    pass


class StageDependencyError(StageFactoryError):
    """Dependência necessária não foi fornecida."""

    pass
