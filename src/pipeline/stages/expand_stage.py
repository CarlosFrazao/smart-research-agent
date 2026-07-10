"""src/pipeline/stages/expand_stage.py

Stage independente do pipeline de pesquisa do Smart Research Agent (SRA)
responsável por expandir a query original do usuário em múltiplas variações
semânticas, delegando o trabalho pesado ao ``QueryExpander`` já existente em
``src/query_expander.py`` e adicionando cache de expansões para evitar
chamadas LLM redundantes (item 23 do plano de correções/melhorias).

CONTEXTO E TRANSPARÊNCIA SOBRE A ANÁLISE DO REPOSITÓRIO
---------------------------------------------------------
Este arquivo foi escrito depois de uma análise real do repositório
``CarlosFrazao/smart-research-agent`` (README.md, README_DE_IMPLEMENTACAO.md,
CHANGELOG.md, SESSION_LOG.md e pyproject.toml). Não foi possível, no entanto,
ler o conteúdo bruto de ``src/query_expander.py`` ou ``src/cache/cache.py``
neste ambiente (a API REST do GitHub, o raw.githubusercontent.com e as
páginas de listagem de diretório ``/tree/...`` estão bloqueados aqui; só
páginas ``blob`` já referenciadas em conteúdo previamente carregado puderam
ser abertas, e o repositório não está indexado publicamente para busca).

Duas consequências práticas dessa limitação, resolvidas de forma defensiva
abaixo:

1. **Sem `PipelineStage`/`PipelineContext` concretos ainda**: pelo
   SESSION_LOG.md, o diretório ``src/pipeline/`` ainda não existe no
   repositório — este item 23 é o primeiro "stage" a ser criado, e depende
   do item 21 (``src/pipeline/pipeline.py``), que também ainda não existe.
   Em vez de importar uma classe que não existe (quebrando o import no
   momento em que este arquivo for adicionado), a interface é expressa via
   ``typing.Protocol`` estrutural. Assim que ``pipeline.py`` for criado com
   as classes reais `PipelineStage`/`PipelineContext`, basta que elas seguam
   o mesmo contrato estrutural (atributo ``query``, atributo mutável
   ``expanded_queries`` e método ``run``) — nenhuma mudança é necessária
   neste arquivo, ou opcionalmente trocam-se os `Protocol` por imports
   diretos.

2. **Assinatura exata do `QueryExpander` desconhecida**: como não consegui
   ler o arquivo fonte, o `ExpandStage` não presume uma assinatura fixa de
   `.expand(...)`. Ele inspeciona o método em tempo de execução (via
   `inspect.signature`) e só passa os kwargs que o método realmente aceita,
   suportando tanto `expand(query)` síncrono quanto assíncrono, e tanto
   `expand(query, intent=...)` quanto `expand(query, domain=...)` etc. Isso
   evita quebrar a integração por causa de um palpite errado de nome de
   parâmetro. Recomendo colar aqui o conteúdo real de
   ``src/query_expander.py`` para eu travar a assinatura exata e simplificar
   este adaptador.

Requisitos do item 23 atendidos:
  * Chama `QueryExpander` (via injeção de dependência + adaptador defensivo).
  * Retorna lista de queries expandidas no `PipelineContext`.
  * Cache de expansões similares (normalização de chave + hook opcional para
    cache semântico/injetado, com TTL e fallback em memória).
"""

from __future__ import annotations

import hashlib
import inspect
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

try:
    import structlog

    logger = structlog.get_logger(__name__)
except ImportError:  # pragma: no cover - fallback se structlog não estiver instalado
    import logging

    logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Contratos estruturais (Protocol) — ver nota de transparência no topo do
# arquivo sobre por que não importamos classes concretas de src/pipeline/.
# --------------------------------------------------------------------------- #
@runtime_checkable
class PipelineContext(Protocol):
    """Contrato mínimo esperado do contexto compartilhado entre stages.

    Compatível por duck typing com o `Context object` descrito no item 21
    do plano (`src/pipeline/pipeline.py`).
    """

    query: str
    intent: str | None
    expanded_queries: list[str]


@runtime_checkable
class QueryExpanderLike(Protocol):
    """Contrato mínimo esperado de `src/query_expander.py::QueryExpander`."""

    def expand(self, query: str, **kwargs: Any) -> Any: ...


@runtime_checkable
class ExpansionCache(Protocol):
    """Contrato mínimo para um backend de cache injetável.

    Compatível por duck typing com `src/cache/cache.py::Cache` (síncrono ou
    assíncrono — ambos são detectados e tratados corretamente) e, no futuro,
    com o cache semântico do item 32 (`src/utils/semantic_cache.py`), desde
    que exponha `get`/`set`. Se o backend também expuser `get_similar`, o
    ExpandStage o usa automaticamente para achar expansões de queries
    parecidas (>= `similarity_threshold`) em vez de só correspondência exata.
    """

    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> Any: ...


@dataclass
class ExpandStageResult:
    """Resultado estruturado produzido por este stage (auditável/testável)."""

    expanded_queries: list[str]
    cache_hit: bool
    cache_key: str
    source: str  # "cache" | "cache_similar" | "query_expander" | "fallback"
    duration_ms: float
    error: str | None = None


class _InMemoryTTLCache:
    """Cache local simples usado apenas quando nenhum backend é injetado.

    Não substitui o cache semântico/compartilhado planejado nos itens 7 e 32
    do plano — serve como fallback seguro para o stage nunca ficar sem
    cache, mesmo antes desses itens serem implementados.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else 3600
        self._store[key] = (time.monotonic() + ttl, value)


def _normalize_query(query: str) -> str:
    """Normaliza a query para aumentar hit-rate de cache entre variações
    triviais (maiúsculas/minúsculas, espaços duplicados, pontuação final).
    """
    normalized = query.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.rstrip("?!. ")
    return normalized


def _build_cache_key(query: str, intent: str | None) -> str:
    normalized = _normalize_query(query)
    raw = f"expand:{intent or ''}:{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _maybe_await(value: Any) -> Any:
    """Aguarda o valor se ele for awaitable; caso contrário, retorna direto.

    Permite que o stage funcione tanto com caches/QueryExpander síncronos
    quanto assíncronos sem duplicar lógica.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class ExpandStage:
    """Stage do pipeline responsável pela expansão de queries (item 23).

    Parameters
    ----------
    query_expander:
        Instância de `QueryExpander` (ou compatível por duck typing). Se
        `None`, o stage tenta importar `src.query_expander.QueryExpander`
        preguiçosamente na primeira execução.
    cache:
        Backend de cache opcional (ex.: `src.cache.cache.Cache`). Se `None`,
        usa um cache local em memória como fallback seguro.
    ttl_seconds:
        TTL padrão das expansões em cache.
    similarity_threshold:
        Repassado ao backend de cache quando ele suporta busca por
        similaridade (`get_similar`), para reaproveitar expansões de queries
        parecidas em vez de recalcular via LLM.
    max_expansions:
        Teto de variações retornadas, independente de quantas o
        QueryExpander gerar (defesa contra explosão de custo).
    """

    name = "expand"

    def __init__(
        self,
        query_expander: QueryExpanderLike | None = None,
        cache: ExpansionCache | None = None,
        ttl_seconds: int = 3600,
        similarity_threshold: float = 0.90,
        max_expansions: int = 10,
    ) -> None:
        self._query_expander = query_expander
        self._cache: ExpansionCache = cache or _InMemoryTTLCache()
        self._ttl_seconds = ttl_seconds
        self._similarity_threshold = similarity_threshold
        self._max_expansions = max_expansions

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa a expansão de query e popula `context.expanded_queries`.

        Nunca propaga exceção do QueryExpander: em caso de falha, aplica
        fallback para `[query original]`, respeitando a "regra de ouro nº5"
        do próprio guia de implementação do projeto ("todo componente deve
        ter fallback; se a API falhar, o sistema não quebra").
        """
        start = time.perf_counter()

        # FASE 6.5: extrai operadores de busca avançada (site:, filetype:,
        # intitle:) da query original ANTES da expansão. O texto limpo alimenta
        # a expansão; os operadores ficam em context.extras para os searchers
        # que os suportam nativamente (SearXNG, DuckDuckGo) os reaplicarem.
        query = context.query
        try:
            from src.query_parser import parse_advanced_query

            parsed = parse_advanced_query(query)
            if parsed.has_operators:
                query = parsed.text or query
                extras = getattr(context, "extras", None)
                if isinstance(extras, dict):
                    extras["advanced_operators"] = {
                        "site_filter": parsed.site_filter,
                        "filetype": parsed.filetype,
                        "intitle": parsed.intitle,
                        "engine_query": parsed.to_engine_query(),
                    }
                logger.info(
                    "expand_stage.operators_parsed",
                    site=parsed.site_filter,
                    filetype=parsed.filetype,
                    intitle=parsed.intitle,
                )
        except Exception as exc:  # noqa: BLE001 - parser nunca deve quebrar o stage
            logger.warning("expand_stage.operator_parse_failed", error=str(exc))

        intent = getattr(context, "intent", None)
        cache_key = _build_cache_key(query, intent)

        result = await self._try_cache(cache_key, query)
        if result is None:
            result = await self._expand_with_llm(query, intent, cache_key, start)

        context.expanded_queries = result.expanded_queries[: self._max_expansions]

        logger.info(
            "expand_stage.done",
            query=query,
            source=result.source,
            cache_hit=result.cache_hit,
            n_expansions=len(context.expanded_queries),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )

        # Planeja fontes se não houver um source_plan no contexto
        if intent is not None:
            from src.types import ExpandedQuery

            sanitized_queries = []
            for q in context.expanded_queries:
                if isinstance(q, ExpandedQuery):
                    sanitized_queries.append(q)
                elif isinstance(q, str):
                    sanitized_queries.append(
                        ExpandedQuery(
                            query=q,
                            type="fallback",
                            priority="media",
                            rationale="fallback",
                        )
                    )
                elif isinstance(q, dict):
                    sanitized_queries.append(
                        ExpandedQuery(
                            query=q.get("query", ""),
                            type=q.get("type", "fallback"),
                            priority=q.get("priority", "media"),
                            rationale=q.get("rationale", ""),
                        )
                    )
            context.expanded_queries = sanitized_queries

            if getattr(context, "source_plan", None) is None:
                from src.source_planner import SourcePlanner

                planner = SourcePlanner()
                context.source_plan = planner.plan(
                    intent, context.expanded_queries, context
                )

            # --- INICIO DO BLOCANTE HUMAN-IN-THE-LOOP (HITL) ---
            orchestrator = context.extras.get("orchestrator")
            session_id = context.extras.get("session_id", "default_session")
            hitl_manager = (
                getattr(orchestrator, "hitl_manager", None) if orchestrator else None
            )

            # Por padrão, HITL está habilitado a não ser que explicitamente desabilitado na config
            hitl_enabled = True
            if orchestrator and hasattr(orchestrator, "config"):
                hitl_enabled = getattr(orchestrator.config, "hitl_enabled", True)

            if hitl_manager and hitl_enabled:
                # Prepara os dados para solicitação de aprovação
                # Serializa o plano e a lista de queries expandidas
                serialized_queries = [
                    {
                        "query": q.query,
                        "type": q.type,
                        "priority": q.priority,
                        "rationale": q.rationale,
                    }
                    if hasattr(q, "query")
                    else {
                        "query": str(q),
                        "type": "expanded",
                        "priority": "alta",
                        "rationale": "",
                    }
                    for q in context.expanded_queries
                ]

                serialized_plan = {}
                if context.source_plan and hasattr(context.source_plan, "sources"):
                    serialized_plan = {
                        "sources": {
                            src: [
                                {
                                    "query": q.query,
                                    "type": q.type,
                                    "priority": q.priority,
                                    "rationale": q.rationale,
                                }
                                if hasattr(q, "query")
                                else {
                                    "query": str(q),
                                    "type": "expanded",
                                    "priority": "alta",
                                    "rationale": "",
                                }
                                for q in q_list
                            ]
                            for src, q_list in context.source_plan.sources.items()
                        },
                        "primary": getattr(context.source_plan, "primary", []),
                        "secondary": getattr(context.source_plan, "secondary", []),
                    }

                hitl_data = {
                    "queries": serialized_queries,
                    "source_plan": serialized_plan,
                }

                logger.info(
                    f"[HITL] Pausando pipeline para aprovação humana na sessão '{session_id}'..."
                )
                approved_data = await hitl_manager.request_approval(
                    session_id=session_id,
                    request_type="source_plan",
                    data=hitl_data,
                    timeout=300.0,  # Timeout de 5 minutos
                )

                # Reconstrói queries expandidas e plano a partir do feedback do usuário
                if approved_data and isinstance(approved_data, dict):
                    logger.info(
                        f"[HITL] Retomando pipeline na sessão '{session_id}' com dados aprovados."
                    )
                    from src.types import ExpandedQuery, SourcePlan

                    # 1. Reconstrói queries
                    edited_queries = approved_data.get("queries")
                    if edited_queries is not None:
                        new_queries = []
                        for eq in edited_queries:
                            if isinstance(eq, str):
                                new_queries.append(
                                    ExpandedQuery(
                                        query=eq,
                                        type="user_approved",
                                        priority="alta",
                                        rationale="Aprovado pelo usuário",
                                    )
                                )
                            elif isinstance(eq, dict):
                                new_queries.append(
                                    ExpandedQuery(
                                        query=eq.get("query", ""),
                                        type=eq.get("type", "user_approved"),
                                        priority=eq.get("priority", "alta"),
                                        rationale=eq.get(
                                            "rationale", "Aprovado pelo usuário"
                                        ),
                                    )
                                )
                        context.expanded_queries = new_queries

                    # 2. Reconstrói plano
                    edited_plan = approved_data.get("source_plan")
                    if edited_plan is not None:
                        sources_dict = {}
                        if "sources" in edited_plan:
                            for src, q_list in edited_plan["sources"].items():
                                expanded_q_list = []
                                for q in q_list:
                                    if isinstance(q, str):
                                        expanded_q_list.append(
                                            ExpandedQuery(
                                                query=q,
                                                type="user_approved",
                                                priority="alta",
                                                rationale="Aprovado pelo usuário",
                                            )
                                        )
                                    elif isinstance(q, dict):
                                        expanded_q_list.append(
                                            ExpandedQuery(
                                                query=q.get("query", ""),
                                                type=q.get("type", "user_approved"),
                                                priority=q.get("priority", "alta"),
                                                rationale=q.get(
                                                    "rationale", "Aprovado pelo usuário"
                                                ),
                                            )
                                        )
                                sources_dict[src] = expanded_q_list

                        context.source_plan = SourcePlan(
                            sources=sources_dict,
                            primary=edited_plan.get("primary", []),
                            secondary=edited_plan.get("secondary", []),
                        )
            # --- FIM DO BLOCANTE HUMAN-IN-THE-LOOP (HITL) ---

        return context

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _try_cache(self, cache_key: str, query: str) -> ExpandStageResult | None:
        start = time.perf_counter()

        cached = await _maybe_await(self._cache.get(cache_key))
        if cached:
            return ExpandStageResult(
                expanded_queries=list(cached),
                cache_hit=True,
                cache_key=cache_key,
                source="cache",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )

        get_similar = getattr(self._cache, "get_similar", None)
        if callable(get_similar):
            try:
                similar = await _maybe_await(
                    get_similar(query, threshold=self._similarity_threshold)
                )
            except TypeError:
                # backend com assinatura diferente de get_similar(query, threshold=...)
                similar = await _maybe_await(get_similar(query))
            if similar:
                return ExpandStageResult(
                    expanded_queries=list(similar),
                    cache_hit=True,
                    cache_key=cache_key,
                    source="cache_similar",
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )

        return None

    async def _expand_with_llm(
        self,
        query: str,
        intent: str | None,
        cache_key: str,
        start: float,
    ) -> ExpandStageResult:
        expander = self._resolve_query_expander()

        try:
            expanded = await self._call_expander(expander, query, intent)
            expanded = self._normalize_result(expanded, query)

            await _maybe_await(
                self._cache.set(cache_key, expanded, ttl=self._ttl_seconds)
            )

            return ExpandStageResult(
                expanded_queries=expanded,
                cache_hit=False,
                cache_key=cache_key,
                source="query_expander",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
        except Exception as exc:  # noqa: BLE001 - fallback obrigatório (regra de ouro nº5)
            logger.warning(
                "expand_stage.fallback",
                query=query,
                error=str(exc),
                exc_info=exc,
            )
            return ExpandStageResult(
                expanded_queries=[query],
                cache_hit=False,
                cache_key=cache_key,
                source="fallback",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                error=str(exc),
            )

    async def _call_expander(
        self, expander: QueryExpanderLike, query: str, intent: str | None
    ) -> Any:
        """Chama `expander.expand(...)` passando apenas os kwargs que o
        método aceita (ver nota de transparência no topo do arquivo sobre a
        assinatura desconhecida do QueryExpander real).
        """
        candidate_kwargs = {
            "intent": intent,
            "domain": intent,
            "max_variations": self._max_expansions,
            "n": self._max_expansions,
            "num_variations": self._max_expansions,
        }

        try:
            signature = inspect.signature(expander.expand)
            accepted = {
                name for name in candidate_kwargs if name in signature.parameters
            }
        except (TypeError, ValueError):
            # builtin/C-extension sem assinatura inspecionável: tenta sem kwargs extras
            accepted = set()

        kwargs = {
            k: v for k, v in candidate_kwargs.items() if k in accepted and v is not None
        }

        result = expander.expand(query, **kwargs)
        return await _maybe_await(result)

    def _resolve_query_expander(self) -> QueryExpanderLike:
        if self._query_expander is not None:
            return self._query_expander

        try:
            from src.query_expander import QueryExpander  # import tardio e opcional
        except ImportError as exc:
            raise RuntimeError(
                "ExpandStage: nenhum QueryExpander foi injetado e "
                "'src.query_expander.QueryExpander' não pôde ser importado. "
                "Injete uma instância via ExpandStage(query_expander=...)."
            ) from exc

        self._query_expander = QueryExpander()
        return self._query_expander

    @staticmethod
    def _normalize_result(expanded: Any, original_query: str) -> list[str]:
        """Normaliza o retorno do QueryExpander para `list[str]`, cobrindo
        os formatos mais comuns (lista de strings, lista de dicts com
        chave 'query', ou um único objeto de resultado com atributo
        `.queries`), já que a assinatura exata é desconhecida (ver nota de
        transparência).
        """
        if expanded is None:
            return [original_query]

        if hasattr(expanded, "queries"):
            expanded = expanded.queries

        if isinstance(expanded, dict) and "queries" in expanded:
            expanded = expanded["queries"]

        if isinstance(expanded, str):
            expanded = [expanded]

        queries: list[str] = []
        for item in expanded:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("query") or item.get("text") or ""
            else:
                text = (
                    getattr(item, "query", None)
                    or getattr(item, "text", None)
                    or str(item)
                )
            text = text.strip()
            if text and text not in queries:
                queries.append(text)

        if original_query not in queries:
            queries.insert(0, original_query)

        return queries or [original_query]


__all__ = ["ExpandStage", "ExpandStageResult", "PipelineContext", "ExpansionCache"]
