"""
ReActOrchestrator — Orquestrador dinâmico com loop ReAct e fallback ao pipeline clássico.

Este módulo estende o Orchestrator existente para suportar o loop ReAct quando
a flag `enable_dynamic_loop=true` em Config. A implementação:

1. Usa DynamicDecisionEngine para decidir dinamicamente quais etapas executar
2. Mantém compatibilidade total com o pipeline clássico sequencial (fallback seguro)
3. Integra avaliação contínua (RAGAS/TruLens) em cada iteração
4. Fornece trace de decisões para o EvidenceGraph e auditoria posterior

Modo de uso no Orchestrator (injeção de dependência):
    - Quando enable_dynamic_loop=False (default): usa ResearchPipeline clássico
    - Quando enable_dynamic_loop=True: usa ReActOrchestrator.loop_run()
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from src.decision_engine import Decision, DynamicDecisionEngine
from src.orchestrator import Orchestrator
from src.pipeline.pipeline import PipelineContext, PipelineError
from src.pipeline.stage_factory import StageFactory
from src.utils.logging import setup_logger

logger = setup_logger("react-orchestrator")


class ReActOrchestrator(Orchestrator):
    """Orquestrador com loop ReAct dinâmico em vez de pipeline sequencial.

    Subclass do Orchestrator principal que substitui o método `research()` por um
    loop adaptativo. Mantém 100% de compatibilidade retroativa: se a flag
    `enable_dynamic_loop=False`, o método `research()` pai é usado automaticamente.
    """

    def __init__(self, config: Any = None) -> None:
        """Inicializa com possível configuração para loop ReAct."""
        super().__init__(config)
        self._react_enabled = getattr(self.config, "enable_dynamic_loop", False)
        # Instância de StageFactory para criar estágios sob demanda no loop
        # ReAct. `create_stage` é um método de instância (não estático), então
        # precisamos de uma factory viva, com as mesmas dependências injetadas
        # que o pipeline clássico usa.
        self._stage_factory = StageFactory(
            orchestrator=self,
            llm_client=self.llm,
            cache=self.cache,
            config=self.config,
        )
        self._decision_engine = DynamicDecisionEngine(
            config=self.config,
            confidence_threshold=getattr(
                self.config, "react_confidence_threshold", 50.0
            ),
            max_iterations=getattr(self.config, "react_max_iterations", 10),
            operation_mode=getattr(self.config, "operation_mode", "cirurgia"),
        )

    async def research(
        self,
        query: str,
        formats: list[Any] | None = None,
        progress_callback: Optional[Callable[..., Any]] = None,
        session_id: str = "default_session",
        context_extra: dict | None = None,
    ) -> str:
        """Executa a pesquisa via loop ReAct se habilitado, senão usa pipeline clássico.

        Args:
            query: Query de pesquisa.
            formats: Formatos de exportação adicionais.
            progress_callback: Callback de progresso (para SSE/streaming).
            session_id: ID da sessão para rastreamento.
            context_extra: Extras opcionais injetados no PipelineContext
                (mantém compatibilidade com a assinatura do Orchestrator pai).

        Returns:
            str: Relatório Markdown completo.
        """
        # Delega ao pai se loop ReAct desabilitado (comportamento clássico)
        if not self._react_enabled:
            return await super().research(
                query, formats, progress_callback, session_id, context_extra
            )

        logger.info(
            "ReActOrchestrator: iniciando loop ReAct para query='%s' (modo=%s).",
            query,
            self.operation_mode.name
            if hasattr(self.operation_mode, "name")
            else self.config.operation_mode,
        )

        # Reset do motor de decisão para nova execução
        self._decision_engine.reset()

        # Contexto inicial
        context = PipelineContext(query=query, formats=formats)
        context.extras["orchestrator"] = self
        context.extras["session_id"] = session_id
        context.extras["progress_callback"] = progress_callback

        # Loop principal ReAct
        decision = self._decision_engine.decide(context)
        while decision.next_stage is not None:
            stage_name = decision.next_stage
            self._decision_engine.mark_executed(stage_name)

            try:
                # Executar estágio via StageFactory (mesmo mecanismo clássico)
                stage = self._stage_factory.create_stage(stage_name)
                context = await self._execute_stage_with_progress(
                    stage, context, progress_callback, session_id
                )
                # Registrar trace de decisão para auditoria
                context.extras.setdefault("react_trace", []).append(
                    self._decision_engine.export_decision_trace()
                )
            except Exception as exc:
                logger.warning(
                    "ReActOrchestrator: estágio '%s' falhou (%s) — continuando.",
                    stage_name,
                    type(exc).__name__,
                )
                context.record_error(stage_name, exc, critical=False)

            # Próxima decisão
            decision = self._decision_engine.decide(context)

        # Finalização: garantir relatório gerado
        if not context.report:
            report_stage = self._stage_factory.create_stage("report")
            context = await self._execute_stage_with_progress(
                report_stage, context, progress_callback, session_id
            )

        logger.info(
            "ReActOrchestrator: loop concluído após %d iterações.",
            self._decision_engine._iteration,
        )
        return context.report

    async def _execute_stage_with_progress(
        self,
        stage: Any,
        context: PipelineContext,
        progress_callback: Optional[Callable[..., Any]],
        session_id: str,
    ) -> PipelineContext:
        """Executa um estágio com notificação de progresso.

        Args:
            stage: Instância de PipelineStage.
            context: Contexto atual.
            progress_callback: Callback para SSE.
            session_id: ID da sessão.

        Returns:
            PipelineContext: Contexto atualizado após a stage.
        """
        # Mapeamento de nomes de stage para passos de progresso
        step_map = {
            "intent": 1,
            "storm": 2,
            "expand": 3,
            "search": 4,
            "rank": 5,
            "verification": 6,
            "graph_explorer": 7,
            "gap": 8,
            "synthesize": 9,
            "report": 10,
            "audit": 11,
        }

        step = step_map.get(stage.name, 0)
        await self._report_progress(
            progress_callback, step, f"ReAct → executando: {stage.name}"
        )

        start = time.monotonic()
        new_context = await stage.run(context)
        duration = time.monotonic() - start
        context.mark_complete(stage.name, duration)

        await self._report_progress(
            progress_callback, step, f"{stage.name} concluído ({duration:.1f}s)"
        )

        return new_context if new_context is not None else context

    async def close_searchers(self) -> None:
        """Fecha searchers após execução ReAct (herdado do Orchestrator)."""
        await super().close_searchers()
        # Opcional: limpar recursos do loop ReAct
        if hasattr(self, "_decision_engine"):
            self._decision_engine.reset()
