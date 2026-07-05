"""
hitl_dialog_agent.py — Agente Interativo de Alinhamento e Negociação HITL

Responsabilidade:
  Implementa uma camada de diálogo dinâmico e contextual entre o pipeline de
  pesquisa e o usuário, permitindo decisões de pivotamento baseadas em achados
  intermediários de alta relevância.

  Diferença do HITLManager existente (src/hitl_manager.py):
    - HITLManager: Gerencia SUSPENSÃO TÉCNICA da task assíncrona e eventos de liberação.
    - HITLDialogAgent: Camada de APRESENTAÇÃO E NEGOCIAÇÃO — analisa achados intermediários,
      decide SE é necessário interromper, formula a pergunta certa e interpreta a resposta.

  Tipos de diálogos suportados:
  - SCOPE_CLARIFICATION: O tema está ambíguo e houve divergência significativa nas sub-queries.
  - PIVOT_DECISION: Achado intermediário contradiz a hipótese inicial; usuário escolhe direção.
  - SOURCE_VETO: Fonte suspeita ou conflitante detectada; usuário aprova/rejeita a inclusão.
  - DEPTH_CONTROL: Pipeline atingiu orçamento de tokens; usuário escolhe aprofundar ou concluir.
  - ALERT_CRITICAL_FINDING: Informação de alta urgência (ex: CVE, recall de produto) exige ação imediata.

Integração:
  - Atua como wrapper inteligente sobre o HITLManager existente.
  - Injeta mensagens de diálogo na UI Streamlit via SSE ou via WebSocket.
  - Formata os diálogos com contexto suficiente para o usuário tomar decisões informadas.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger("hitl_dialog_agent")


# ─── Enums e Constantes ────────────────────────────────────────────────────────

class DialogType(StrEnum):
    """Tipo de interrupção de diálogo que o agente pode gerar."""
    SCOPE_CLARIFICATION = "scope_clarification"
    PIVOT_DECISION = "pivot_decision"
    SOURCE_VETO = "source_veto"
    DEPTH_CONTROL = "depth_control"
    ALERT_CRITICAL_FINDING = "alert_critical_finding"

# Score mínimo de urgência para interromper automaticamente o pipeline
URGENCY_THRESHOLD_AUTO_INTERRUPT = 0.75
# Tempo máximo de espera por resposta do usuário em segundos
DEFAULT_DIALOG_TIMEOUT = 180.0  # 3 minutos


# ─── Data Contracts ────────────────────────────────────────────────────────────

@dataclass
class DialogTurn:
    """Um turno de diálogo entre o agente e o usuário."""
    dialog_id: str
    session_id: str
    dialog_type: DialogType
    question: str
    context: str  # Achado ou situação que motivou a pergunta
    options: list[str]  # Opções de resposta sugeridas para o usuário
    urgency_score: float  # 0.0-1.0 (alto = interrompe automaticamente)
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    user_response: str | None = None
    was_timeout: bool = False

    @property
    def is_pending(self) -> bool:
        """Indica se o diálogo ainda aguarda resposta do usuário."""
        return self.user_response is None and not self.was_timeout

    @property
    def wait_time_seconds(self) -> float | None:
        """Tempo de espera pela resposta do usuário, se já foi respondido."""
        if self.answered_at is None:
            return None
        return self.answered_at - self.created_at


@dataclass
class DialogDecision:
    """Decisão interpretada pelo agente a partir da resposta do usuário."""
    dialog_id: str
    action: str         # Ação concreta a ser tomada pelo pipeline
    parameters: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    auto_resolved: bool = False  # True se foi resolvido por timeout ou fallback automático


@dataclass
class HITLDialogReport:
    """Relatório de todas as interações de diálogo em uma sessão de pesquisa."""
    session_id: str
    dialogs: list[DialogTurn] = field(default_factory=list)
    decisions: list[DialogDecision] = field(default_factory=list)
    total_wait_time_seconds: float = 0.0
    auto_resolved_count: int = 0

    @property
    def total_dialogs(self) -> int:
        return len(self.dialogs)

    @property
    def user_engagement_rate(self) -> float:
        """Taxa de diálogos respondidos pelo usuário (vs. resolvidos por timeout)."""
        if not self.dialogs:
            return 0.0
        user_answered = sum(1 for d in self.dialogs if not d.was_timeout)
        return round(user_answered / len(self.dialogs), 4)


# ─── Agente Principal ──────────────────────────────────────────────────────────

class HITLDialogAgent:
    """Agente de diálogo interativo e contextual para alinhamento HITL no pipeline SRA.

    Analisa achados intermediários, determina se uma interrupção é necessária,
    formula perguntas contextuais e interpreta as respostas do usuário como
    ações concretas de pivotamento no pipeline de pesquisa.

    Uso básico:
        agent = HITLDialogAgent(hitl_manager=orchestrator.hitl_manager, llm=orchestrator.llm)
        # Após achado controverso:
        dialog = await agent.evaluate_finding(session_id="abc", finding=achado)
        if dialog:
            decision = await agent.await_user_decision(dialog, timeout=120.0)
            # Aplicar decision.action ao pipeline
    """

    def __init__(self, hitl_manager: Any = None, llm: Any = None) -> None:
        self._hitl = hitl_manager
        self._llm = llm
        self._active_dialogs: dict[str, DialogTurn] = {}
        self._dialog_history: list[DialogTurn] = {}  # type: ignore[assignment]
        self._dialog_history = []
        logger.info(
            f"HITLDialogAgent inicializado. "
            f"HITLManager: {'conectado' if hitl_manager else 'ausente (modo passivo)'}. "
            f"LLM: {'disponível' if llm else 'ausente (templates fixos)'}."
        )

    async def evaluate_finding(
        self,
        session_id: str,
        finding: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> DialogTurn | None:
        """Avalia um achado intermediário e decide se uma interrupção HITL é necessária.

        Args:
            session_id: ID da sessão de pesquisa ativa.
            finding: Dicionário com chaves: 'type', 'content', 'urgency' (0.0-1.0).
            context: Contexto adicional da pesquisa (query original, etapa atual, etc.).

        Returns:
            DialogTurn se uma interrupção foi gerada, None se o pipeline pode continuar.
        """
        urgency = float(finding.get("urgency", 0.0))
        finding_type = finding.get("type", "general")
        content = finding.get("content", "")

        if urgency < URGENCY_THRESHOLD_AUTO_INTERRUPT:
            logger.debug(
                f"[HITLDialog] Achado com urgência {urgency:.2f} abaixo do threshold. "
                "Pipeline continua sem interrupção."
            )
            return None

        dialog_type = self._map_finding_to_dialog_type(finding_type)
        dialog = await self._create_dialog(
            session_id=session_id,
            dialog_type=dialog_type,
            finding_content=content,
            urgency=urgency,
            context=context or {},
        )

        self._active_dialogs[dialog.dialog_id] = dialog
        self._dialog_history.append(dialog)
        logger.info(
            f"[HITLDialog] Diálogo gerado para sessão {session_id}: "
            f"tipo={dialog_type.value}, urgência={urgency:.2f}"
        )
        return dialog

    def _map_finding_to_dialog_type(self, finding_type: str) -> DialogType:
        """Mapeia o tipo de achado para o tipo de diálogo mais adequado."""
        mapping = {
            "contradiction": DialogType.PIVOT_DECISION,
            "ambiguity": DialogType.SCOPE_CLARIFICATION,
            "suspicious_source": DialogType.SOURCE_VETO,
            "budget_limit": DialogType.DEPTH_CONTROL,
            "critical_alert": DialogType.ALERT_CRITICAL_FINDING,
            "cve": DialogType.ALERT_CRITICAL_FINDING,
            "conflict": DialogType.PIVOT_DECISION,
        }
        return mapping.get(finding_type, DialogType.PIVOT_DECISION)

    async def _create_dialog(
        self,
        session_id: str,
        dialog_type: DialogType,
        finding_content: str,
        urgency: float,
        context: dict[str, Any],
    ) -> DialogTurn:
        """Cria um DialogTurn com pergunta contextual formulada pelo LLM ou por template."""
        import uuid
        dialog_id = f"dialog_{uuid.uuid4().hex[:8]}"

        question, options = await self._generate_question_and_options(
            dialog_type, finding_content, context
        )

        return DialogTurn(
            dialog_id=dialog_id,
            session_id=session_id,
            dialog_type=dialog_type,
            question=question,
            context=finding_content[:800],
            options=options,
            urgency_score=urgency,
        )

    async def _generate_question_and_options(
        self,
        dialog_type: DialogType,
        content: str,
        context: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Gera a pergunta e as opções de resposta via LLM ou templates fixos."""
        templates = {
            DialogType.SCOPE_CLARIFICATION: (
                f"O escopo da pesquisa divergiu em múltiplas direções. "
                f"Achado intermediário: '{content[:200]}'. "
                "Qual direção prefere aprofundar?",
                ["Manter o foco original", "Expandir para incluir o novo ângulo", "Criar pesquisa separada para o novo ângulo"],
            ),
            DialogType.PIVOT_DECISION: (
                f"Um achado contradiz a hipótese inicial. Evidência: '{content[:200]}'. "
                "Como deseja proceder?",
                ["Incluir a contradição no relatório", "Ignorar e manter hipótese original", "Aprofundar a investigação da contradição"],
            ),
            DialogType.SOURCE_VETO: (
                f"Fonte com credibilidade questionável detectada: '{content[:200]}'. "
                "Como proceder?",
                ["Incluir com ressalva explícita", "Excluir da pesquisa", "Buscar fontes alternativas que corroborem"],
            ),
            DialogType.DEPTH_CONTROL: (
                f"O orçamento de tokens está próximo do limite. Contexto atual: '{content[:200]}'. "
                "Como proceder?",
                ["Encerrar e gerar relatório com o que temos", "Aprofundar somente no subtema mais relevante", "Continuar e aceitar custo adicional"],
            ),
            DialogType.ALERT_CRITICAL_FINDING: (
                f"🚨 ALERTA CRÍTICO: '{content[:200]}'. "
                "Este achado exige sua atenção imediata. Como proceder?",
                ["Priorizar este ponto no relatório", "Incluir como seção de alerta urgente", "Solicitar análise mais detalhada antes de continuar"],
            ),
        }

        if self._llm:
            try:
                query = context.get("query", "")
                prompt = (
                    f"Você é um assistente de pesquisa. Uma situação surgiu durante a pesquisa sobre: '{query}'.\n"
                    f"Situação: {dialog_type.value}\n"
                    f"Detalhes: {content[:400]}\n\n"
                    "Formule uma pergunta clara e objetiva para o usuário tomar uma decisão. "
                    "Também liste exatamente 3 opções de resposta objetivas, separadas por '|'. "
                    "Formato: PERGUNTA\nOPÇÃO1|OPÇÃO2|OPÇÃO3"
                )
                response = await self._llm.complete(prompt, max_tokens=200)
                lines = response.strip().split("\n")
                if len(lines) >= 2:
                    question = lines[0].strip()
                    options_raw = lines[-1].strip()
                    options = [o.strip() for o in options_raw.split("|") if o.strip()]
                    if question and len(options) >= 2:
                        return question, options[:4]
            except Exception as e:
                logger.debug(f"[HITLDialog] Falha no LLM ao gerar pergunta: {e}")

        # Fallback para template fixo
        return templates.get(dialog_type, (f"Revisão necessária: {content[:200]}", ["Continuar", "Pausar"]))

    async def await_user_decision(
        self,
        dialog: DialogTurn,
        timeout: float = DEFAULT_DIALOG_TIMEOUT,
    ) -> DialogDecision:
        """Suspende o pipeline e aguarda a decisão do usuário para um diálogo ativo.

        Args:
            dialog: DialogTurn gerado por evaluate_finding.
            timeout: Timeout em segundos antes de auto-resolver com fallback.

        Returns:
            DialogDecision com a ação a ser tomada pelo pipeline.
        """
        if self._hitl is None:
            logger.warning("[HITLDialog] HITLManager não disponível. Auto-resolvendo diálogo.")
            return self._auto_resolve(dialog, reason="hitl_manager_unavailable")

        try:
            data_for_approval = {
                "dialog_id": dialog.dialog_id,
                "question": dialog.question,
                "context": dialog.context,
                "options": dialog.options,
                "dialog_type": dialog.dialog_type.value,
                "urgency_score": dialog.urgency_score,
            }
            response = await self._hitl.request_approval(
                session_id=dialog.session_id,
                request_type=f"hitl_dialog_{dialog.dialog_type.value}",
                data=data_for_approval,
                timeout=timeout,
            )
            dialog.user_response = str(response) if response != data_for_approval else None
            dialog.answered_at = time.time()

            if dialog.user_response is None or response == data_for_approval:
                # Timeout — HITLManager retornou o data original sem modificação
                dialog.was_timeout = True
                return self._auto_resolve(dialog, reason="timeout")

            decision = self._interpret_response(dialog, dialog.user_response)
            self._cleanup_dialog(dialog.dialog_id)
            return decision

        except Exception as e:
            logger.error(f"[HITLDialog] Erro ao aguardar decisão do usuário: {e}")
            return self._auto_resolve(dialog, reason=f"error: {e}")

    def _interpret_response(self, dialog: DialogTurn, response: str) -> DialogDecision:
        """Interpreta a resposta do usuário e converte em uma ação concreta para o pipeline."""
        response_lower = response.lower().strip()
        action = "continue"  # Default seguro
        params: dict[str, Any] = {"user_response": response, "dialog_type": dialog.dialog_type.value}

        if dialog.dialog_type == DialogType.PIVOT_DECISION:
            if "contradição" in response_lower or "investigar" in response_lower or "aprofundar" in response_lower:
                action = "pivot_to_contradiction"
                params["additional_query"] = f"evidência contradição: {dialog.context[:100]}"
            elif "ignorar" in response_lower or "manter" in response_lower:
                action = "maintain_original_scope"
            else:
                action = "include_with_note"

        elif dialog.dialog_type == DialogType.SOURCE_VETO:
            if "excluir" in response_lower:
                action = "exclude_source"
            elif "ressalva" in response_lower:
                action = "include_with_caveat"
            else:
                action = "find_alternative_sources"

        elif dialog.dialog_type == DialogType.DEPTH_CONTROL:
            if "encerrar" in response_lower or "gerar" in response_lower:
                action = "finalize_report"
            elif "custo" in response_lower or "continuar" in response_lower:
                action = "extend_budget"
            else:
                action = "focus_top_subtopic"

        elif dialog.dialog_type == DialogType.ALERT_CRITICAL_FINDING:
            action = "prioritize_critical_finding"

        elif dialog.dialog_type == DialogType.SCOPE_CLARIFICATION:
            if "expandir" in response_lower:
                action = "expand_scope"
            elif "separada" in response_lower:
                action = "create_separate_research"
            else:
                action = "maintain_original_scope"

        return DialogDecision(
            dialog_id=dialog.dialog_id,
            action=action,
            parameters=params,
            confidence=0.9,
        )

    def _auto_resolve(self, dialog: DialogTurn, reason: str) -> DialogDecision:
        """Resolve automaticamente um diálogo sem resposta do usuário (timeout ou erro)."""
        logger.info(
            f"[HITLDialog] Diálogo {dialog.dialog_id} auto-resolvido. Motivo: {reason}."
        )
        dialog.was_timeout = True
        self._cleanup_dialog(dialog.dialog_id)
        return DialogDecision(
            dialog_id=dialog.dialog_id,
            action="continue",
            parameters={"auto_resolve_reason": reason},
            confidence=0.5,
            auto_resolved=True,
        )

    def _cleanup_dialog(self, dialog_id: str) -> None:
        """Remove o diálogo da lista de ativos."""
        self._active_dialogs.pop(dialog_id, None)

    def get_active_dialogs(self) -> list[DialogTurn]:
        """Retorna a lista de diálogos atualmente aguardando resposta do usuário."""
        return list(self._active_dialogs.values())

    def get_report(self, session_id: str) -> HITLDialogReport:
        """Retorna o relatório de interações de diálogo para uma sessão específica."""
        session_dialogs = [d for d in self._dialog_history if d.session_id == session_id]
        total_wait = sum(
            d.wait_time_seconds for d in session_dialogs if d.wait_time_seconds is not None
        )
        auto_resolved = sum(1 for d in session_dialogs if d.was_timeout)
        return HITLDialogReport(
            session_id=session_id,
            dialogs=session_dialogs,
            total_wait_time_seconds=total_wait,
            auto_resolved_count=auto_resolved,
        )

    async def create_alert(
        self,
        session_id: str,
        alert_message: str,
        urgency: float = 0.95,
    ) -> DialogTurn:
        """Atalho para criar um alerta crítico imediato sem avaliação de achado.

        Args:
            session_id: ID da sessão de pesquisa.
            alert_message: Mensagem de alerta a ser exibida para o usuário.
            urgency: Nível de urgência (default: 0.95 = máxima prioridade).

        Returns:
            DialogTurn com o alerta criado.
        """
        finding = {
            "type": "critical_alert",
            "content": alert_message,
            "urgency": urgency,
        }
        return await self.evaluate_finding(session_id=session_id, finding=finding)
