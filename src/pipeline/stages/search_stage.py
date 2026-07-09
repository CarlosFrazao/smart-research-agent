"""Search Stage — Stage independente para execução paralela de buscas.

Responsabilidades:
  - Orquestrar buscas em múltiplos searchers com concorrência controlada.
  - Aplicar Circuit Breaker por source para evitar cascata de falhas.
  - Early termination: aborta se N resultados de alta qualidade forem coletados.
  - Integração com cache (TTL adaptativo por source).
  - Ranqueamento híbrido dos resultados brutos.

Design:
  - Recebe dependências via construtor (DI) — não depende do Orchestrator.
  - Semaphore por source: limita concorrência por fonte (default 3-5).
  - TaskGroup + asyncio.gather com cancelamento cooperativo para early termination.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.query_validator import QueryValidator
from src.ranker import QualityRanker
from src.types import RankedResult, SearchResult, SourcePlan
from src.utils.circuit_breaker import (
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
)

logger = logging.getLogger("pipeline.search_stage")


# ── Source Classification for Sanitization (FASE 0.4) ────────────────────────────

# Fontes de alta confiança — isentas de sanitização (APIs estruturadas)
TRUSTED_SOURCES = frozenset({
    "github", "arxiv", "pubmed", "semantic_scholar",
    "hackernews", "stackoverflow", "reddit", "rss",
    "awesome", "wayback", "producthunt",
})

# Fontes não-confiáveis — texto livre, scraping, redes sociais
UNTRUSTED_SOURCES = frozenset({
    "firecrawl", "scraping", "searxng", "web",
    "multilingual", "playwright", "spider", "steel",
    "duckduckgo", "quora", "twitter", "telegram",
    # Novos desta fase:
    "discourse",  # texto livre de fórum
    "google_trends",  # dados numéricos, mas origem externa
    "google_patents",  # conteúdo raspado de páginas de patentes
    "mercadolivre",  # descrições de vendedores (texto livre)
})


@dataclass
class SearchStageConfig:
    """Configuração fina do SearchStage."""

    # Concorrência
    max_concurrent_per_source: int = 3  # Semaphore por source (3-5)
    global_timeout: float = 120.0  # Timeout total do stage

    # Early termination
    early_termination_enabled: bool = True
    early_termination_threshold: float = (
        0.80  # Score mínimo para contar como "alta qualidade"
    )
    early_termination_count: int = 15  # N resultados de alta qualidade para parar

    # Circuit breaker
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 60.0

    # Cache
    cache_enabled: bool = True
    cache_ttl_override: Optional[int] = None  # Se None, usa TTL adaptativo do Cache

    # Fallback
    fallback_on_empty: bool = True  # Usa SerpAPI se nenhum resultado


class SearchStage(PipelineStage):
    """Stage de busca paralela com semáforo, circuit breaker e early termination.

    Args:
        searchers: Mapa {source_name: BaseSearcher} (injetado).
        cache: Instância de Cache (injetado).
        ranker: Instância de QualityRanker (injetado).
        config: SearchStageConfig com parâmetros operacionais.
        circuit_breaker_registry: Registro compartilhado de circuit breakers.
        health_monitor: Opcional, para reportar falhas.
    """

    name = "search"

    def __init__(
        self,
        searchers: Dict[str, Any],
        cache: Any,
        ranker: QualityRanker,
        config: Optional[SearchStageConfig] = None,
        circuit_breaker_registry: Optional[Any] = None,
        health_monitor: Optional[Any] = None,
        sanitizer: Optional[Any] = None,
    ):
        self.searchers = searchers
        self.cache = cache
        self.ranker = ranker
        self.config = config or SearchStageConfig()
        self.cb_registry = circuit_breaker_registry or CircuitBreakerRegistry
        self.health_monitor = health_monitor
        self.sanitizer = sanitizer

        # Semaphore por source (limita concorrência por fonte)
        self._semaphores: Dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(self.config.max_concurrent_per_source)
            for name in searchers
        }

        # Tracking de early termination
        self._high_quality_count = 0
        self._stop_event = asyncio.Event()

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa buscas paralelas, ranqueia e retorna contexto enriquecido.

        Fluxo:
          1. Valida contexto (query, source_plan, intent).
          2. Monta tasks de busca com cache-hit short-circuit.
          3. Executa com asyncio.gather + cancelamento se early termination ativar.
          4. Fallback SerpAPI se vazio.
          5. Ranqueia todos os resultados brutos.
          6. Atualiza context.raw_results e context.ranked_results.
        """
        if not context.source_plan:
            raise ValueError(
                "PipelineContext.source_plan é obrigatório para SearchStage"
            )
        if not context.intent:
            raise ValueError("PipelineContext.intent é obrigatório para SearchStage")

        if context.metadata is None:
            context.metadata = {}

        logger.info(
            f"SearchStage: Iniciando busca para fontes {list(context.source_plan.sources.keys())} "
            f"com {len(context.expanded_queries)} queries."
        )

        self._stop_event.clear()
        self._high_quality_count = 0

        # 1. Coletar tasks de busca (cache-aware)
        tasks, cached_results = await self._build_tasks(context)
        all_results: List[SearchResult] = list(cached_results)

        # 2. Executar tasks em paralelo com early termination
        if tasks:
            gathered = await self._execute_with_early_termination(tasks, context)
            all_results.extend(gathered)

        # Injetar eventos do StreamMonitorAgent se disponível
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        if orchestrator and getattr(orchestrator, "stream_monitor", None):
            try:
                stream_results = await orchestrator.stream_monitor.events_as_search_results(
                    limit=10
                )
                if stream_results:
                    all_results.extend(stream_results)
                    logger.info(
                        "SearchStage: %d eventos do StreamMonitorAgent injetados.",
                        len(stream_results),
                    )
            except Exception as e:
                logger.warning(
                    f"SearchStage: falha ao injetar eventos do StreamMonitorAgent: {e}"
                )

        # 3. Fallback de último recurso
        if not all_results and self.config.fallback_on_empty:
            fallback_results = await self._fallback_serpapi(context)
            all_results.extend(fallback_results)

        # 4. Ranqueamento
        ranked = await self.ranker.rank(all_results)
        # Garante que são RankedResult (QualityRanker já retorna RankedResult)
        ranked = [r for r in ranked if isinstance(r, RankedResult)]

        # 5. Atualizar contexto
        context.raw_results = all_results
        context.ranked_results = ranked
        context.metadata["search"] = {
            "sources_queried": len(tasks) + len(cached_results),
            "total_raw": len(all_results),
            "total_ranked": len(ranked),
            "high_quality_triggered": self._high_quality_count,
            "circuit_breakers": self.cb_registry.metrics_all(),
        }

        logger.info(
            f"SearchStage concluído: {len(all_results)} brutos, {len(ranked)} ranqueados, "
            f"early_stop={self._stop_event.is_set()}"
        )
        return context

    async def _build_tasks(
        self, context: PipelineContext
    ) -> tuple[List[asyncio.Task], List[SearchResult]]:
        """Monta lista de tasks e separa resultados já cacheados.

        Returns:
            (tasks, cached_results): Tasks a executar + resultados do cache.
        """
        tasks: List[asyncio.Task] = []
        cached_results: List[SearchResult] = []
        plan: SourcePlan = context.source_plan
        intent = context.intent

        for source_name, source_queries in plan.sources.items():
            # Filtro por modo de operação (se presente no contexto)
            allowed_searchers = context.metadata.get("allowed_searchers")
            if allowed_searchers and source_name not in allowed_searchers:
                logger.debug(f"Source '{source_name}' filtrado pelo modo de operação")
                continue

            searcher = self.searchers.get(source_name)
            if not searcher or not getattr(searcher, "enabled", True):
                continue

            for eq in source_queries:
                sanitized = QueryValidator.sanitize(eq.query)
                if not QueryValidator.is_valid(sanitized):
                    logger.warning(f"Query inválida descartada: '{eq.query[:50]}'")
                    continue
                eq.query = sanitized

                cache_key = f"{source_name}:{eq.query}"
                if self.config.cache_enabled and self.cache:
                    try:
                        cached = await self.cache.get("search", cache_key)
                        if cached is not None:
                            logger.debug(f"Cache hit: {cache_key}")
                            deserialized = self._deserialize_cached(cached)
                            cached_results.extend(deserialized)
                            continue
                    except Exception as e:
                        logger.warning(f"Erro ao ler cache para {cache_key}: {e}")

                task = asyncio.create_task(
                    self._search_with_protection(
                        searcher, source_name, eq.query, intent.domain.value
                    ),
                    name=f"{source_name}:{eq.query[:30]}",
                )
                tasks.append(task)

        return tasks, cached_results

    async def _execute_with_early_termination(
        self, tasks: List[asyncio.Task], context: PipelineContext
    ) -> List[SearchResult]:
        """Executa tasks com monitoramento de early termination.

        Usa asyncio.gather com return_exceptions=True e verifica
        a cada resultado se o threshold de early termination foi atingido.
        """
        results: List[SearchResult] = []
        pending = set(tasks)

        while pending and not self._stop_event.is_set():
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self.config.global_timeout,
            )
            if not done:
                logger.warning(
                    "SearchStage: timeout global atingido, cancelando pendentes"
                )
                break

            for task in done:
                try:
                    res = task.result()
                    if isinstance(res, Exception):
                        logger.error(f"Task {task.get_name()} falhou: {res}")
                        continue
                    source_name, query_str, task_results = res
                    if task_results:
                        results.extend(task_results)
                        # Early termination check
                        if self.config.early_termination_enabled:
                            for r in task_results:
                                if (
                                    getattr(r, "confidence_score", 0.0)
                                    >= self.config.early_termination_threshold
                                ):
                                    self._high_quality_count += 1
                            if (
                                self._high_quality_count
                                >= self.config.early_termination_count
                            ):
                                logger.info(
                                    f"Early termination: {self._high_quality_count} resultados "
                                    f"de alta qualidade >= {self.config.early_termination_threshold}"
                                )
                                self._stop_event.set()
                                break
                except Exception as e:
                    logger.error(f"Erro ao processar resultado de task: {e}")

            # Salvaguarda de volume máximo de resultados
            if len(results) >= 50:
                logger.info(
                    "Early termination secundário: limite máximo de 50 resultados atingido."
                )
                self._stop_event.set()

            if self._stop_event.is_set():
                break

        # Cancela pendentes se early termination ou timeout
        if pending:
            for task in pending:
                task.cancel()
            # Aguarda cancelamento para evitar warnings
            await asyncio.gather(*pending, return_exceptions=True)

        return results

    async def _search_with_protection(
        self, searcher: Any, source_name: str, query: str, domain: str
    ) -> tuple[str, str, List[SearchResult]]:
        """Executa busca com circuit breaker + semaphore + timeout.

        Returns:
            (source_name, query, results)
        """
        cb = self.cb_registry.get(
            source_name,
            failure_threshold=self.config.circuit_breaker_failure_threshold,
            recovery_timeout=self.config.circuit_breaker_recovery_timeout,
        )
        sem = self._semaphores.get(source_name)

        try:
            async with sem if sem else asyncio.Semaphore(999):
                result = await cb.call(
                    self._search_with_timeout, searcher, query, domain, source_name
                )
                # FASE 0.4: Sanitizar descrições de fontes não-confiáveis
                if self.sanitizer and source_name in UNTRUSTED_SOURCES:
                    result = await self._sanitize_results(result, source_name)
                return source_name, query, result
        except CircuitBreakerOpen:
            logger.warning(f"Circuit breaker OPEN para '{source_name}' — pulando")
            return source_name, query, []
        except Exception as e:
            logger.error(f"Erro protegido em '{source_name}': {e}")
            return source_name, query, []

    async def _sanitize_results(
        self, results: List[SearchResult], source_name: str
    ) -> List[SearchResult]:
        """Sanitiza descrições de resultados de fontes não-confiáveis.

        Aplica o LLMSanitizer apenas a descrições longas (>100 chars) para
        detectar e neutralizar tentativas de prompt injection.
        """
        if not self.sanitizer or not results:
            return results

        for result in results:
            desc = result.get("description", "")
            if desc and len(desc) > 100:
                try:
                    sanitized = await self.sanitizer.sanitize(desc)
                    if sanitized.was_injection_detected:
                        logger.warning(
                            "[SEGURANÇA] Prompt injection detectado em '%s' URL=%s",
                            source_name, result.get("url", ""),
                        )
                    # Atualiza a descrição com o conteúdo sanitizado
                    if hasattr(result, 'description'):
                        result.description = sanitized.cleaned
                    elif isinstance(result, dict):
                        result["description"] = sanitized.cleaned
                except Exception as e:
                    logger.warning(f"Falha ao sanitizar resultado de '{source_name}': {e}")

        return results

    async def _search_with_timeout(
        self, searcher: Any, query: str, domain: str, source_name: str
    ) -> List[SearchResult]:
        """Executa busca com timeout e fallback do próprio searcher."""
        timeout = getattr(searcher, "timeout", 30)
        if not isinstance(timeout, (int, float)):
            timeout = 30.0
        try:
            results = await asyncio.wait_for(
                searcher.search(query, domain=domain),
                timeout=timeout,
            )
            # Cache write
            if self.config.cache_enabled and self.cache and results:
                try:
                    await self.cache.set(
                        "search",
                        f"{source_name}:{query}",
                        [
                            r.model_dump() if hasattr(r, "model_dump") else r.__dict__
                            for r in results
                        ],
                    )
                except Exception as e:
                    logger.warning(
                        f"Erro ao escrever cache para {source_name}:{query}: {e}"
                    )
            return results
        except asyncio.TimeoutError:
            logger.warning(f"Timeout em {source_name} (>{timeout}s)")
            if self.health_monitor:
                self.health_monitor.report_failure(source_name, "TimeoutError")
            # Fallback do searcher
            if hasattr(searcher, "fallback") and callable(searcher.fallback):
                try:
                    return searcher.fallback(query)
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.error(f"Exceção em {source_name}: {e}")
            if self.health_monitor:
                self.health_monitor.report_failure(source_name, str(e))
            raise

    async def _fallback_serpapi(self, context: PipelineContext) -> List[SearchResult]:
        """Fallback de último recurso via SerpAPI se configurado."""
        serpapi = self.searchers.get("serpapi")
        if not serpapi or not getattr(serpapi, "is_available", True):
            return []

        queries = [q.query for q in context.expanded_queries if q.priority == "alta"]
        if not queries and context.expanded_queries:
            queries = [context.expanded_queries[0].query]
        if not queries:
            queries = [context.query]

        from urllib.parse import urlparse

        all_results: List[SearchResult] = []
        for q_str in queries:
            try:
                res = await serpapi.search(q_str)
                if res:
                    for r in res:
                        all_results.append(
                            SearchResult(
                                source="serpapi",
                                title=r.get("title", ""),
                                url=r.get("url", ""),
                                description=r.get("snippet", ""),
                                metrics={
                                    "source_domain": urlparse(r.get("url", "")).netloc
                                },
                                raw=r,
                            )
                        )
            except Exception as e:
                logger.error(f"Fallback SerpAPI falhou para '{q_str}': {e}")
        return all_results

    @staticmethod
    def _deserialize_cached(cached: List[Dict[str, Any]]) -> List[SearchResult]:
        """Converte lista de dicts do cache em SearchResult."""
        results = []
        for r in cached:
            if not isinstance(r, dict):
                continue
            # Restaura datetime se necessário
            if "fetched_at" in r and isinstance(r["fetched_at"], str):
                try:
                    r["fetched_at"] = datetime.fromisoformat(r["fetched_at"])
                except Exception:
                    from datetime import datetime as _dt

                    r["fetched_at"] = _dt.now()
            try:
                results.append(SearchResult(**r))
            except Exception as e:
                logger.warning(f"Falha ao desserializar resultado do cache: {e}")
        return results
