"""src/pipeline/pipeline.py — Motor genérico do Pipeline Pattern do SRA.

Este módulo é a peça de infraestrutura central da refatoração do
`Orchestrator` (God Object, ~655 linhas) descrita no item 1 do plano de
correções. Ele NÃO conhece nenhuma etapa concreta de pesquisa (intent,
expand, search, rank, ...) — essas vivem em `src/pipeline/stages/*`
(itens 22-29) e são montadas em tempo de execução pela `StageFactory`
(item 40). `pipeline.py` só sabe executar uma lista de `PipelineStage`
em sequência, propagando um `PipelineContext` entre elas.

Decisões de design
-------------------
1. **`PipelineContext` é um DTO puro.** Ele carrega *dados* (query,
   resultados intermediários, metadados), nunca *serviços* (LLMClient,
   searchers, etc.). Cada `PipelineStage` recebe suas dependências via
   construtor (Dependency Injection — ver item 42, `dependencies.py`),
   não via contexto. Isso evita que o contexto vire um novo God Object
   disfarçado e mantém cada stage testável isoladamente com mocks.

2. **Falha crítica vs. não-crítica.** O `Orchestrator` atual já tem esse
   conceito implícito espalhado em ~10 blocos `try/except Exception:
   logger.warning(...)` (EvidenceGraph, PeerReview, ConflictDetector,
   ResearchScoreAggregator etc. são "best effort"; intent/search/rank
   são obrigatórios). Aqui isso vira um atributo explícito da stage
   (`critical: bool`), eliminando a duplicação e tornando a política
   auditável em um único lugar.

3. **Rollback é best-effort e não mascara a exceção original.** Se uma
   stage crítica falha, o pipeline desfaz (em ordem reversa) as stages
   já concluídas chamando `stage.rollback(context)`. Falhas durante o
   rollback são logadas mas nunca substituem a exceção que motivou o
   abort — o chamador sempre recebe a causa raiz real.

4. **Contexto parcial sobrevive à falha.** `PipelineError` carrega o
   `context` no estado em que estava no momento do abort, permitindo
   que o chamador (ex.: `FallbackManager`, item 41) recupere resultados
   parciais em vez de perder tudo.

Exemplo de uso
--------------
    stages = [
        IntentStage(intent_analyzer),
        ExpandStage(query_expander),
        SearchStage(search_service),
        RankStage(reasoning_service),
        SynthesizeStage(synthesizer),
        ReportStage(report_service),
    ]
    pipeline = ResearchPipeline(stages, dlq=dead_letter_queue)
    context = await pipeline.run("Rust async best practices")
    print(context.report)
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.exceptions import SRABaseError
from src.utils.logging import setup_logger

logger = setup_logger("pipeline")

__all__ = [
    "PipelineContext",
    "PipelineStage",
    "PipelineError",
    "StageError",
    "ResearchPipeline",
    "Pipeline",
]


# ─────────────────────────────────────────────────────────────────────────
# Context — DTO propagado entre stages
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class StageError:
    """Registro de uma falha ocorrida durante a execução de uma stage.

    Attributes:
        stage: Nome da stage que falhou (`PipelineStage.name`).
        error_type: Nome da classe da exceção capturada.
        message: Mensagem da exceção.
        critical: Se a falha era de uma stage crítica (abortou o pipeline).
        timestamp: Momento em que a falha foi registrada.
    """

    stage: str
    error_type: str
    message: str
    critical: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class PipelineContext:
    """Estado compartilhado entre as stages de uma execução do pipeline.

    Campos tipados cobrem os artefatos centrais do fluxo de pesquisa
    descrito no plano (intent -> expand -> search -> rank -> score ->
    gap -> synthesize -> report). Artefatos adicionais/experimentais
    (evidence graph, conflict report, peer review, research score etc.)
    vivem em `extra` para não exigir alterar este arquivo toda vez que
    uma nova stage opcional é criada.

    Os tipos concretos (`IntentResult`, `SourcePlan`, `RankedResult`, ...)
    são definidos em `src/types.py`; aqui usamos `Any` para não acoplar
    o motor genérico do pipeline ao domínio de pesquisa.
    """

    query: str
    enriched_query: str = ""
    formats: list[Any] | None = None
    started_at: datetime = field(default_factory=datetime.now)

    # Artefatos do fluxo principal (preenchidos progressivamente pelas stages)
    intent: Any | None = None
    expanded_queries: list[Any] = field(default_factory=list)
    source_plan: Any | None = None
    raw_results: list[Any] = field(default_factory=list)
    ranked_results: list[Any] = field(default_factory=list)
    gap_analysis: Any | None = None
    synthesized_results: list[Any] = field(default_factory=list)
    metadata: Any | None = None
    report: str = ""

    # Catch-all para artefatos não previstos neste core (evidence_graph,
    # conflict_report, peer_review_report, research_score, iterations, ...)
    extra: dict[str, Any] = field(default_factory=dict)

    # Telemetria de execução
    completed_stages: list[str] = field(default_factory=list)
    errors: list[StageError] = field(default_factory=list)
    stage_durations: dict[str, float] = field(default_factory=dict)

    @property
    def extras(self) -> dict[str, Any]:
        """Alias para retrocompatibilidade com chamadas a extras."""
        return self.extra

    @extras.setter
    def extras(self, value: dict[str, Any]) -> None:
        self.extra = value

    @property
    def ranked(self) -> list[Any]:
        """Alias para retrocompatibilidade com ranked_results."""
        return self.ranked_results

    @ranked.setter
    def ranked(self, value: list[Any]) -> None:
        self.ranked_results = value

    @property
    def raw(self) -> list[Any]:
        """Alias para retrocompatibilidade com raw_results."""
        return self.raw_results

    @raw.setter
    def raw(self, value: list[Any]) -> None:
        self.raw_results = value

    # ── Helpers ─────────────────────────────────────────────────────────

    def elapsed_seconds(self) -> float:
        """Tempo total decorrido desde o início da execução do pipeline."""
        return (datetime.now() - self.started_at).total_seconds()

    def mark_complete(self, stage_name: str, duration_seconds: float) -> None:
        """Registra a conclusão bem-sucedida de uma stage."""
        self.completed_stages.append(stage_name)
        self.stage_durations[stage_name] = duration_seconds

    def record_error(self, stage_name: str, exc: Exception, critical: bool) -> None:
        """Registra uma falha de stage sem interromper o fluxo de dados."""
        self.errors.append(
            StageError(
                stage=stage_name,
                error_type=type(exc).__name__,
                message=str(exc),
                critical=critical,
            )
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Acesso conveniente a `extra` (artefatos não tipados)."""
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Escrita conveniente em `extra` (artefatos não tipados)."""
        self.extra[key] = value


# ─────────────────────────────────────────────────────────────────────────
# Stage — contrato que cada etapa concreta do pipeline deve implementar
# ─────────────────────────────────────────────────────────────────────────


class PipelineStage(ABC):
    """Interface base para uma etapa do pipeline de pesquisa.

    Subclasses concretas (`IntentStage`, `SearchStage`, `RankStage`, ...)
    recebem suas dependências (LLMClient, searchers, serviços) no
    `__init__` — nunca leem serviços do `PipelineContext`, que é
    estritamente um DTO de dados (ver docstring do módulo).

    Attributes:
        name: Identificador legível da stage, usado em logs, telemetria
            e como chave de lookup para rollback. Deve ser único dentro
            de um mesmo `ResearchPipeline`.
        critical: Se ``True`` (default), uma falha nesta stage aborta o
            pipeline inteiro (com rollback). Se ``False``, a falha é
            logada e a execução prossegue para a próxima stage — mesma
            semântica dos blocos "best effort" atualmente espalhados
            pelo `Orchestrator` (EvidenceGraph, PeerReview, etc.).
        max_retries: Número de novas tentativas em caso de falha antes
            de considerar a stage definitivamente falha. ``0`` (default)
            desabilita retry. Backoff simples e linear; para retry com
            backoff exponencial/jitter e integração com circuit breaker,
            ver `src/utils/retry.py` (item 31), que pode substituir este
            mecanismo básico sem alterar o contrato de `PipelineStage`.
        retry_backoff_seconds: Base do backoff linear entre tentativas.
    """

    name: str = "unnamed_stage"
    critical: bool = True
    max_retries: int = 0
    retry_backoff_seconds: float = 1.0

    @abstractmethod
    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa a lógica da stage, mutando e/ou retornando o contexto.

        Args:
            context: Estado acumulado pelas stages anteriores.

        Returns:
            PipelineContext: Contexto atualizado (pode ser o mesmo objeto
                mutado in-place, ou um novo — o pipeline usa o valor
                retornado como fonte de verdade para a próxima stage).

        Raises:
            Exception: Qualquer exceção sinaliza falha da stage. O
                `ResearchPipeline` decide o que fazer com base em
                `critical`.
        """
        raise NotImplementedError

    async def rollback(self, context: PipelineContext) -> None:
        """Desfaz efeitos colaterais desta stage após abort do pipeline.

        No-op por padrão — a maioria das stages de pesquisa é somente
        leitura (busca, ranqueamento, síntese) e não precisa de rollback.
        Sobrescreva para stages com efeitos colaterais reais (ex.: uma
        stage que grava em memória persistente ou dispara uma tarefa
        externa deve desfazer isso aqui).
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PipelineStage '{self.name}' critical={self.critical}>"


# ─────────────────────────────────────────────────────────────────────────
# Erros
# ─────────────────────────────────────────────────────────────────────────


class PipelineError(SRABaseError):
    """Levantado quando uma stage crítica falha e o pipeline é abortado.

    Attributes:
        stage: Nome da stage que causou o abort.
        cause: Exceção original capturada.
        context: `PipelineContext` no estado em que estava no momento do
            abort — permite ao chamador recuperar resultados parciais
            (ex.: `ranked_results` já calculados mesmo que a síntese
            tenha falhado) em vez de perder toda a execução.
    """

    def __init__(
        self, message: str, *, stage: str, cause: Exception, context: PipelineContext
    ):
        super().__init__(message)
        self.stage = stage
        self.cause = cause
        self.context = context


# ─────────────────────────────────────────────────────────────────────────
# ResearchPipeline — orquestra a execução sequencial das stages
# ─────────────────────────────────────────────────────────────────────────


class ResearchPipeline:
    """Executa uma lista ordenada de `PipelineStage` propagando um contexto.

    Substitui a sequência de chamadas hardcoded do `Orchestrator.research()`
    (`_plan_search` -> `_execute_searches` -> `_synthesize_results`) por uma
    lista declarativa e componível de stages, montada externamente pela
    `StageFactory` (item 40) a partir de `OperationConfig` (permite, por
    exemplo, montar um pipeline sem `PeerReviewStage` no modo "guerrilha").

    Não faz nenhuma chamada de rede, LLM ou I/O diretamente — apenas
    orquestra: sequenciamento, telemetria, política crítico/não-crítico,
    retry básico e rollback. Toda lógica de domínio vive nas stages.
    """

    def __init__(
        self,
        stages: Sequence[PipelineStage],
        *,
        name: str = "research_pipeline",
        dlq: Any | None = None,
    ) -> None:
        """
        Args:
            stages: Lista ordenada de stages a executar sequencialmente.
            name: Identificador do pipeline, usado em logs.
            dlq: Instância opcional de `DeadLetterQueue` (`src/utils/
                dead_letter_queue.py`). Se fornecida, falhas críticas são
                persistidas para análise/retry posterior antes de serem
                propagadas como `PipelineError`.

        Raises:
            ValueError: Se houver nomes de stage duplicados (necessário
                para o lookup de rollback ser não-ambíguo).
        """
        self.stages: list[PipelineStage] = list(stages)
        self.name = name
        self.dlq = dlq

        names = [s.name for s in self.stages]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"Nomes de stage duplicados em '{name}': {sorted(duplicates)}. "
                "Cada PipelineStage.name deve ser único dentro do pipeline."
            )
        self._by_name: dict[str, PipelineStage] = {s.name: s for s in self.stages}

    def describe(self) -> str:
        """Representação legível da ordem de execução (útil em logs/debug)."""
        parts = [
            f"{i + 1}. {s.name}{'' if getattr(s, 'critical', True) else ' (opcional)'}"
            for i, s in enumerate(self.stages)
        ]
        return f"ResearchPipeline '{self.name}': " + " -> ".join(parts)

    def add_stage(self, stage: PipelineStage, *, index: int | None = None) -> None:
        """Insere uma stage na composição (usado por `StageFactory`/`FallbackManager`).

        Args:
            stage: Stage a inserir.
            index: Posição de inserção. Se ``None``, adiciona ao final.

        Raises:
            ValueError: Se já existir uma stage com o mesmo nome.
        """
        if stage.name in self._by_name:
            raise ValueError(f"Stage '{stage.name}' já existe neste pipeline.")
        if index is None:
            self.stages.append(stage)
        else:
            self.stages.insert(index, stage)
        self._by_name[stage.name] = stage

    async def run(
        self,
        query: Union[str, PipelineContext],
        *,
        formats: list[Any] | None = None,
        enriched_query: str | None = None,
        **initial_extra: Any,
    ) -> PipelineContext:
        """Executa todas as stages em sequência sobre um `PipelineContext`.

        Se `query` for uma instância de `PipelineContext`, ela será usada
        diretamente. Caso contrário, um novo contexto será criado.
        """
        if isinstance(query, PipelineContext):
            context = query
            if formats is not None:
                context.formats = formats
            if enriched_query is not None:
                context.enriched_query = enriched_query
        else:
            context = PipelineContext(
                query=query,
                enriched_query=enriched_query or query,
                formats=formats,
            )
        context.extra.update(initial_extra)

        logger.info(f"[{self.name}] iniciando execução — {self.describe()}")

        for stage in self.stages:
            stage_start = time.monotonic()
            try:
                new_context = await self._run_stage_with_retry(stage, context)
                if new_context is not None:
                    context = new_context
                duration = time.monotonic() - stage_start
                context.mark_complete(stage.name, duration)
                logger.info(
                    f"[{self.name}] stage '{stage.name}' concluída em {duration:.2f}s"
                )
            except Exception as exc:
                is_crit = getattr(stage, "critical", True)
                context.record_error(stage.name, exc, critical=is_crit)

                if not is_crit:
                    logger.warning(
                        f"[{self.name}] stage não-crítica '{stage.name}' falhou "
                        f"({type(exc).__name__}: {exc}) — pipeline prossegue."
                    )
                    continue

                logger.error(
                    f"[{self.name}] stage crítica '{stage.name}' falhou "
                    f"({type(exc).__name__}: {exc}) — abortando pipeline."
                )
                await self._rollback(context)
                if self.dlq is not None:
                    await self._push_to_dlq(stage.name, query, exc)

                raise PipelineError(
                    f"Pipeline '{self.name}' abortado na stage '{stage.name}': {exc}",
                    stage=stage.name,
                    cause=exc,
                    context=context,
                ) from exc

        logger.info(
            f"[{self.name}] execução concluída em {context.elapsed_seconds():.2f}s "
            f"({len(context.completed_stages)}/{len(self.stages)} stages, "
            f"{len(context.errors)} erro(s) não-crítico(s))"
        )
        return context

    # ── Internos ────────────────────────────────────────────────────────

    async def _run_stage_with_retry(
        self, stage: PipelineStage, context: PipelineContext
    ) -> PipelineContext:
        """Executa `stage.run` com retry linear básico (ver `max_retries`)."""
        attempt = 0
        while True:
            try:
                return await stage.run(context)
            except Exception:
                attempt += 1
                max_r = getattr(stage, "max_retries", 0)
                if attempt > max_r:
                    raise
                wait = getattr(stage, "retry_backoff_seconds", 1.0) * attempt
                logger.warning(
                    f"[{self.name}] stage '{stage.name}' falhou "
                    f"(tentativa {attempt}/{max_r}) — "
                    f"nova tentativa em {wait:.1f}s"
                )
                await asyncio.sleep(wait)

    async def _rollback(self, context: PipelineContext) -> None:
        """Desfaz, em ordem reversa, as stages já concluídas.

        Falhas durante o próprio rollback são logadas e ignoradas — elas
        nunca devem mascarar a exceção original que motivou o abort.
        """
        for stage_name in reversed(context.completed_stages):
            stage = self._by_name.get(stage_name)
            if stage is None:
                continue
            try:
                await stage.rollback(context)
                logger.info(f"[{self.name}] rollback aplicado: '{stage_name}'")
            except Exception as rollback_exc:
                logger.error(
                    f"[{self.name}] rollback de '{stage_name}' também falhou "
                    f"(ignorado): {rollback_exc}"
                )

    async def _push_to_dlq(self, stage_name: str, query: str, exc: Exception) -> None:
        """Persiste a falha crítica na DeadLetterQueue, se configurada."""
        try:
            task = self.dlq.create_failed_task(
                task_type="pipeline_stage",
                payload={"query": query, "stage": stage_name},
                error=str(exc),
                source=stage_name,
            )
            await self.dlq.push(task)
        except Exception as dlq_exc:  # pragma: no cover - defensivo
            logger.error(f"[{self.name}] falha ao persistir na DLQ: {dlq_exc}")


# Alias de compatibilidade com stage_factory.py e orquestrador
Pipeline = ResearchPipeline
