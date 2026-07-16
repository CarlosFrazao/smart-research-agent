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
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.query_validator import QueryValidator
from src.ranker import QualityRanker
from src.types import RankedResult, SearchResult, SourcePlan, generate_result_id
from src.utils.circuit_breaker import (
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
)

logger = logging.getLogger("pipeline.search_stage")


# ── Source Classification for Sanitization (FASE 0.4) ────────────────────────────

# Fontes de alta confiança — isentas de sanitização (APIs estruturadas)
TRUSTED_SOURCES = frozenset(
    {
        "github",
        "arxiv",
        "pubmed",
        "semantic_scholar",
        "hackernews",
        "stackoverflow",
        "reddit",
        "rss",
        "awesome",
        "wayback",
        "producthunt",
    }
)

# Fontes não-confiáveis — texto livre, scraping, redes sociais
UNTRUSTED_SOURCES = frozenset(
    {
        "firecrawl",
        "scraping",
        "searxng",
        "web",
        "multilingual",
        "playwright",
        "spider",
        "steel",
        "duckduckgo",
        "quora",
        "twitter",
        "telegram",
        # Novos desta fase:
        "discourse",  # texto livre de fórum
        "google_trends",  # dados numéricos, mas origem externa
        "google_patents",  # conteúdo raspado de páginas de patentes
        "mercadolivre",  # descrições de vendedores (texto livre)
    }
)


# ── SLAs de Timeout Diferenciados por Categoria de Fonte (FASE 5) ───────────────
# Timeouts em segundos. APIs estruturadas rápidas recebem SLA curto; fontes de
# scraping/agentes recebem SLA maior. Fontes não mapeadas caem no default por
# categoria (confiável → _default_api, não-confiável/scraping → _default_scraping).

SOURCE_TIMEOUT_MAP: dict[str, float] = {
    # APIs estruturadas rápidas
    "github": 8.0,
    "arxiv": 8.0,
    "hackernews": 6.0,
    "wikipedia": 5.0,
    "duckduckgo": 5.0,
    "npm": 5.0,
    "pypi": 5.0,
    "cratesio": 5.0,
    "appstore": 5.0,
    "newsapi": 8.0,
    "courtlistener": 10.0,
    "sec_edgar": 12.0,
    # Agregadores e fontes médias
    "reddit": 10.0,
    "producthunt": 10.0,
    "rss": 8.0,
    "mercadolivre": 10.0,
    # Scraping/agentes — timeout maior
    "firecrawl": 30.0,
    "spider": 25.0,
    "steel": 25.0,
    "quora": 20.0,
    "google_patents": 20.0,
    "discourse": 15.0,
    # Default para fontes não mapeadas
    "_default_api": 10.0,
    "_default_scraping": 25.0,
}


def get_timeout_for_source(source_name: str) -> float:
    """Retorna o timeout (em segundos) para uma fonte específica.

    Resolve o SLA por categoria: fontes mapeadas explicitamente usam seu valor;
    fontes não mapeadas caem no default de scraping (se não-confiáveis) ou no
    default de API (caso contrário).
    """
    if source_name in SOURCE_TIMEOUT_MAP:
        return SOURCE_TIMEOUT_MAP[source_name]
    if source_name in UNTRUSTED_SOURCES:
        return SOURCE_TIMEOUT_MAP["_default_scraping"]
    return SOURCE_TIMEOUT_MAP["_default_api"]


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

        # 4.1 (FASE 5): Aplicar regras de allowlist/denylist do usuário
        # (context.extra["trust_rules"] = {source: "allow"|"deny"}). Preenche
        # result.trust_tier e, se configurado, remove fontes "deny".
        all_results = self._apply_trust_rules(all_results, context)

        # Injetar eventos do StreamMonitorAgent se disponível
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        if orchestrator and getattr(orchestrator, "stream_monitor", None):
            try:
                stream_results = (
                    await orchestrator.stream_monitor.events_as_search_results(limit=10)
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

        # FEAT-003 (Resiliência Bloco 3): avisos de transparência de busca.
        # Coleta fontes do plano que não foram atendidas (sem searcher
        # registrado ou sem credencial) para expor ao usuário no relatório.
        search_warnings: List[str] = list(context.extra.get("search_warnings", []))

        for source_name, source_queries in plan.sources.items():
            # Filtro por modo de operação (se presente no contexto)
            allowed_searchers = context.metadata.get("allowed_searchers")
            if allowed_searchers and source_name not in allowed_searchers:
                logger.debug(f"Source '{source_name}' filtrado pelo modo de operação")
                continue

            searcher = self.searchers.get(source_name)
            if not searcher:
                logger.warning(
                    "Source '%s' is in the search plan but has no registered searcher. "
                    "Check SearcherFactory.create_searchers() and Config credentials.",
                    source_name,
                )
                search_warnings.append(
                    f"Fonte '{source_name}' no plano não tem searcher registrado "
                    f"(sem credencial/config)."
                )
                continue
            if not getattr(searcher, "enabled", True):
                continue

            # FEAT-003: searcher presente, mas credencial essencial ausente.
            searcher_credentials = getattr(searcher, "has_credentials", None)
            if searcher_credentials is False:
                logger.warning(
                    "Source '%s' registrado, mas sem credencial configurada. "
                    "A busca será pulada/limitada — verifique a env var correspondente.",
                    source_name,
                )
                search_warnings.append(
                    f"Fonte '{source_name}' sem credencial configurada — "
                    f"busca indisponível ou limitada."
                )
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
                            for r in deserialized:
                                r.result_id = generate_result_id(source_name, r.url)
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

        # FEAT-003: persiste avisos de transparência no contexto para o
        # report_stage expor no rodapé do relatório. Preserva avisos já
        # existentes em context.extra["search_warnings"] (acumula, não sobrescreve).
        if search_warnings:
            context.extra["search_warnings"] = list(search_warnings)

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
                # 4.1: Gerar result_id canônico para cada resultado
                for r in result:
                    r.result_id = generate_result_id(source_name, r.url or "")
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
            # `result` é sempre um `SearchResult` (modelo pydantic) — acesso por
            # atributo, nunca por `.get()` (que só existe em dict e dispara
            # AttributeError em SearchResult).
            desc = getattr(result, "description", "") or ""
            if desc and len(desc) > 100:
                try:
                    sanitized = await self.sanitizer.sanitize(desc)
                    if sanitized.was_injection_detected:
                        logger.warning(
                            "[SEGURANÇA] Prompt injection detectado em '%s' URL=%s",
                            source_name,
                            getattr(result, "url", "") or "",
                        )
                    # Atualiza a descrição com o conteúdo sanitizado.
                    result.description = sanitized.cleaned
                except Exception as e:
                    logger.warning(
                        f"Falha ao sanitizar resultado de '{source_name}': {e}"
                    )

        return results

    async def _search_with_timeout(
        self, searcher: Any, query: str, domain: str, source_name: str
    ) -> List[SearchResult]:
        """Executa busca com timeout e fallback do próprio searcher.

        O timeout é resolvido por categoria de fonte via
        ``get_timeout_for_source`` (SLA diferenciado da Fase 5), em vez do
        valor fixo do atributo ``searcher.timeout``.
        """
        timeout = get_timeout_for_source(source_name)
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
                    return await searcher.fallback(query)
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
        # Gerar result_id para resultados do fallback
        for r in all_results:
            r.result_id = generate_result_id("serpapi", r.url or "")
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

    # ── FASE 5: Allowlist/Denylist (trust_rules) ─────────────────────────────

    def _apply_trust_rules(
        self, results: List[SearchResult], context: PipelineContext
    ) -> List[SearchResult]:
        """Aplica as regras de confiança do usuário aos resultados brutos.

        Lê ``context.extra["trust_rules"]`` (mapa ``{source: "allow"|"deny"}``)
        e preenche ``result.trust_tier`` para cada resultado. Fontes marcadas
        como "deny" são removidas quando ``FILTER_DENIED_SOURCES=true``
        (default), garantindo que o usuário nunca receba conteúdo de uma fonte
        que ele explicitamente bloqueou.

        Args:
            results: Resultados brutos vindos dos searchers.
            context: Contexto do pipeline (de onde vem as ``trust_rules``).

        Returns:
            List[SearchResult]: Resultados com ``trust_tier`` preenchido; as
            fontes "deny" são filtradas conforme a flag de ambiente.
        """
        trust_rules = {}
        extras = (
            getattr(context, "extras", None) or getattr(context, "extra", None) or {}
        )
        raw_rules = extras.get("trust_rules", {})
        if isinstance(raw_rules, dict):
            # Normaliza chaves (fontes) para lowercase para matching robusto.
            for source, tier in raw_rules.items():
                if tier in ("allow", "deny", "neutral"):
                    trust_rules.setdefault(str(source).strip().lower(), tier)

        if not trust_rules:
            return results

        filter_denied = (
            os.environ.get("FILTER_DENIED_SOURCES", "true").lower() == "true"
        )

        filtered: List[SearchResult] = []
        for r in results:
            source_key = (getattr(r, "source", "") or "").strip().lower()
            tier = trust_rules.get(source_key, "neutral")
            try:
                r.trust_tier = tier
            except Exception as e:  # pydantic validation / attr shielding
                logger.debug(f"Não foi possível setar trust_tier em {r}: {e}")
            if tier == "deny":
                if filter_denied:
                    logger.debug("Resultado de fonte negada '%s' excluído.", source_key)
                    continue
                logger.debug(
                    "Resultado de fonte negada '%s' mantido (filtro desabilitado).",
                    source_key,
                )
            filtered.append(r)

        denied = len(results) - len(filtered)
        if denied:
            logger.info(
                "SearchStage: %d resultado(s) de fontes 'deny' removidos por trust_rules.",
                denied,
            )
        return filtered
