"""
test_hitl_dialog_agent.py — Testes unitarios para HITLDialogAgent

Cobre:
  1. Bug principal: PIVOT_DECISION nao confunde "incluir contradicao" com "aprofundar"
  2. _match_option_index: casamento exato, por contencao e nenhum
  3. _extract_response_text: str simples, dict com chaves conhecidas, fallback
  4. evaluate_finding: urgencia abaixo do threshold -> None; acima -> DialogTurn
  5. await_user_decision: modo passivo (sem HITLManager) -> auto_resolve + records decision
  6. await_user_decision: timeout (response is data) -> was_timeout + records decision
  7. await_user_decision: resposta real -> decisao correta + records decision
  8. get_report: decisions populadas corretamente
  9. _notify_dialog_created: callback sincrono e assíncrono
 10. pause_feed / resume_feed analogos: _get_session_lock devolve mesmo lock
 11. _interpret_response: todos os tipos de dialogo (SOURCE_VETO, DEPTH_CONTROL, SCOPE_CLARIFICATION, ALERT)
 12. HITLDialogReport.total_decisions e total_dialogs
 13. create_alert: retorna DialogTurn ou None
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.hitl_dialog_agent import (
    DEFAULT_DIALOG_TIMEOUT,
    URGENCY_THRESHOLD_AUTO_INTERRUPT,
    DialogDecision,
    DialogTurn,
    DialogType,
    HITLDialogAgent,
    HITLDialogReport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(**kwargs) -> HITLDialogAgent:
    return HITLDialogAgent(**kwargs)


def _make_dialog(
    dialog_type: DialogType = DialogType.PIVOT_DECISION,
    options: list[str] | None = None,
    session_id: str = "s1",
) -> DialogTurn:
    return DialogTurn(
        dialog_id="d1",
        session_id=session_id,
        dialog_type=dialog_type,
        question="Qual acao tomar?",
        context="contexto de teste",
        options=options or ["Incluir com nota", "Ignorar achado", "Aprofundar investigacao"],
        urgency_score=0.9,
    )


# ---------------------------------------------------------------------------
# Testes: _match_option_index
# ---------------------------------------------------------------------------

class TestMatchOptionIndex:
    OPTS = ["Incluir a contradicao no relatorio", "Ignorar e manter escopo", "Aprofundar a investigacao"]

    def test_exact_match_first_option(self):
        idx = HITLDialogAgent._match_option_index(self.OPTS, "incluir a contradicao no relatorio")
        assert idx == 0

    def test_exact_match_third_option(self):
        idx = HITLDialogAgent._match_option_index(self.OPTS, "aprofundar a investigacao")
        assert idx == 2

    def test_containment_match(self):
        # Resposta que contem a opcao inteira
        idx = HITLDialogAgent._match_option_index(self.OPTS, "quero ignorar e manter escopo")
        assert idx == 1

    def test_no_match_returns_none(self):
        idx = HITLDialogAgent._match_option_index(self.OPTS, "resposta completamente diferente xyz")
        assert idx is None

    def test_empty_options_returns_none(self):
        assert HITLDialogAgent._match_option_index([], "qualquer coisa") is None


# ---------------------------------------------------------------------------
# Testes: _extract_response_text
# ---------------------------------------------------------------------------

class TestExtractResponseText:
    def test_str_passthrough(self):
        assert HITLDialogAgent._extract_response_text("opcao 1") == "opcao 1"

    def test_dict_selected_option(self):
        result = HITLDialogAgent._extract_response_text({"selected_option": "opcao 2"})
        assert result == "opcao 2"

    def test_dict_user_response_key(self):
        result = HITLDialogAgent._extract_response_text({"user_response": "minha resposta"})
        assert result == "minha resposta"

    def test_dict_fallback_str(self):
        result = HITLDialogAgent._extract_response_text({"unknown_key": 42})
        assert "42" in result or "unknown_key" in result  # str(dict)

    def test_other_types_str(self):
        result = HITLDialogAgent._extract_response_text(99)
        assert result == "99"


# ---------------------------------------------------------------------------
# Testes: Bug principal — PIVOT_DECISION nao confunde opcoes com palavras compartilhadas
# ---------------------------------------------------------------------------

class TestPivotDecisionNoBugAmbiguidade:
    """
    Bug original: "Incluir a contradicao no relatorio" continha a palavra
    "contradicao", que era o gatilho para pivot_to_contradiction.
    Agora _match_option_index resolve por indice exato antes das keywords.
    """

    OPTIONS = [
        "Incluir a contradicao no relatorio",  # idx=0 -> include_with_note
        "Ignorar e manter escopo original",     # idx=1 -> maintain_original_scope
        "Aprofundar a investigacao",            # idx=2 -> pivot_to_contradiction
    ]

    def _make_pivot_dialog(self) -> DialogTurn:
        return DialogTurn(
            dialog_id="d_pivot",
            session_id="s1",
            dialog_type=DialogType.PIVOT_DECISION,
            question="O que fazer?",
            context="contradicao detectada",
            options=self.OPTIONS,
            urgency_score=0.9,
        )

    def _agent(self) -> HITLDialogAgent:
        return _make_agent()

    def test_incluir_contradicao_maps_to_include_with_note(self):
        agent = self._agent()
        dialog = self._make_pivot_dialog()
        decision = agent._interpret_response(dialog, "Incluir a contradicao no relatorio")
        assert decision.action == "include_with_note", (
            f"Bug ainda presente! 'incluir a contradicao' foi mapeado para {decision.action!r}"
        )

    def test_aprofundar_maps_to_pivot_to_contradiction(self):
        agent = self._agent()
        dialog = self._make_pivot_dialog()
        decision = agent._interpret_response(dialog, "Aprofundar a investigacao")
        assert decision.action == "pivot_to_contradiction"

    def test_ignorar_maps_to_maintain_scope(self):
        agent = self._agent()
        dialog = self._make_pivot_dialog()
        decision = agent._interpret_response(dialog, "Ignorar e manter escopo original")
        assert decision.action == "maintain_original_scope"

    def test_default_is_include_with_note(self):
        agent = self._agent()
        dialog = self._make_pivot_dialog()
        decision = agent._interpret_response(dialog, "algo completamente diferente")
        assert decision.action == "include_with_note"


# ---------------------------------------------------------------------------
# Testes: outros tipos de dialogo em _interpret_response
# ---------------------------------------------------------------------------

class TestInterpretResponseOtherTypes:
    def test_source_veto_excluir(self):
        agent = _make_agent()
        opts = ["Incluir com ressalva", "Excluir fonte", "Buscar alternativas"]
        dialog = DialogTurn("d", "s", DialogType.SOURCE_VETO, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Excluir fonte").action == "exclude_source"

    def test_source_veto_ressalva(self):
        agent = _make_agent()
        opts = ["Incluir com ressalva", "Excluir fonte", "Buscar alternativas"]
        dialog = DialogTurn("d", "s", DialogType.SOURCE_VETO, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Incluir com ressalva").action == "include_with_caveat"

    def test_source_veto_default_buscar_alternativas(self):
        agent = _make_agent()
        opts = ["Incluir com ressalva", "Excluir fonte", "Buscar alternativas"]
        dialog = DialogTurn("d", "s", DialogType.SOURCE_VETO, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "resposta estranha").action == "find_alternative_sources"

    def test_depth_control_encerrar(self):
        agent = _make_agent()
        opts = ["Encerrar e gerar relatorio", "Focar no subtopico principal", "Continuar e ampliar budget"]
        dialog = DialogTurn("d", "s", DialogType.DEPTH_CONTROL, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Encerrar e gerar relatorio").action == "finalize_report"

    def test_depth_control_budget(self):
        agent = _make_agent()
        opts = ["Encerrar e gerar relatorio", "Focar no subtopico principal", "Continuar e ampliar budget"]
        dialog = DialogTurn("d", "s", DialogType.DEPTH_CONTROL, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Continuar e ampliar budget").action == "extend_budget"

    def test_scope_clarification_expandir(self):
        agent = _make_agent()
        opts = ["Manter escopo original", "Expandir escopo", "Criar pesquisa separada"]
        dialog = DialogTurn("d", "s", DialogType.SCOPE_CLARIFICATION, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Expandir escopo").action == "expand_scope"

    def test_scope_clarification_separada(self):
        agent = _make_agent()
        opts = ["Manter escopo original", "Expandir escopo", "Criar pesquisa separada"]
        dialog = DialogTurn("d", "s", DialogType.SCOPE_CLARIFICATION, "q", "ctx", opts, 0.9)
        assert agent._interpret_response(dialog, "Criar pesquisa separada").action == "create_separate_research"

    def test_alert_critical_always_prioritize(self):
        agent = _make_agent()
        dialog = DialogTurn("d", "s", DialogType.ALERT_CRITICAL_FINDING, "q", "ctx", [], 0.99)
        assert agent._interpret_response(dialog, "qualquer coisa").action == "prioritize_critical_finding"


# ---------------------------------------------------------------------------
# Testes: evaluate_finding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_finding_below_threshold_returns_none():
    agent = _make_agent()
    result = await agent.evaluate_finding(
        session_id="s1",
        finding={"type": "contradiction", "content": "...", "urgency": 0.5},
    )
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_finding_above_threshold_returns_dialog():
    agent = _make_agent()
    result = await agent.evaluate_finding(
        session_id="s1",
        finding={"type": "contradiction", "content": "Conflito detectado", "urgency": 0.9},
    )
    assert result is not None
    assert isinstance(result, DialogTurn)
    assert result.session_id == "s1"
    assert result.dialog_type == DialogType.PIVOT_DECISION


@pytest.mark.asyncio
async def test_evaluate_finding_callback_invoked():
    called_with = []

    def sync_cb(dialog: DialogTurn):
        called_with.append(dialog)

    agent = _make_agent(dialog_callback=sync_cb)
    await agent.evaluate_finding(
        session_id="s1",
        finding={"type": "contradiction", "content": "x", "urgency": 0.9},
    )
    assert len(called_with) == 1
    assert isinstance(called_with[0], DialogTurn)


@pytest.mark.asyncio
async def test_evaluate_finding_async_callback_invoked():
    called = []

    async def async_cb(dialog: DialogTurn):
        called.append(dialog)

    agent = _make_agent(dialog_callback=async_cb)
    await agent.evaluate_finding(
        session_id="s1",
        finding={"type": "contradiction", "content": "x", "urgency": 0.9},
    )
    assert len(called) == 1


# ---------------------------------------------------------------------------
# Testes: await_user_decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_await_user_decision_passive_mode_auto_resolves_and_records():
    """Sem HITLManager, modo passivo auto-resolve E registra a decisao."""
    agent = _make_agent()
    dialog = _make_dialog()
    agent._active_dialogs[dialog.dialog_id] = dialog
    agent._dialog_history.append(dialog)

    decision = await agent.await_user_decision(dialog)

    assert isinstance(decision, DialogDecision)
    assert decision.auto_resolved is True
    # Decisao deve estar registrada no historico da sessao
    report = agent.get_report(dialog.session_id)
    assert len(report.decisions) == 1
    assert report.decisions[0].dialog_id == dialog.dialog_id


@pytest.mark.asyncio
async def test_await_user_decision_timeout_records_decision():
    """Quando HITLManager retorna o mesmo objeto (timeout por identidade), registra decisao."""
    mock_hitl = AsyncMock()

    # Simula timeout: request_approval devolve o MESMO objeto (identidade)
    async def fake_request_approval(session_id, request_type, data, timeout):
        return data  # mesmo objeto -> `response is data_for_approval`

    mock_hitl.request_approval = fake_request_approval

    agent = _make_agent(hitl_manager=mock_hitl)
    dialog = _make_dialog()
    agent._active_dialogs[dialog.dialog_id] = dialog
    agent._dialog_history.append(dialog)

    decision = await agent.await_user_decision(dialog, timeout=0.1)

    assert decision.auto_resolved is True
    assert dialog.was_timeout is True
    report = agent.get_report(dialog.session_id)
    assert len(report.decisions) == 1


@pytest.mark.asyncio
async def test_await_user_decision_real_response_records_decision():
    """Resposta real do usuario gera decisao correta e e registrada."""
    mock_hitl = AsyncMock()
    response_holder = []

    async def fake_request_approval(session_id, request_type, data, timeout):
        # Devolve objeto DIFERENTE (nao identico ao data_for_approval)
        return "Aprofundar a investigacao"

    mock_hitl.request_approval = fake_request_approval

    agent = _make_agent(hitl_manager=mock_hitl)
    dialog = _make_dialog(
        dialog_type=DialogType.PIVOT_DECISION,
        options=[
            "Incluir a contradicao no relatorio",
            "Ignorar e manter escopo",
            "Aprofundar a investigacao",
        ],
    )
    agent._active_dialogs[dialog.dialog_id] = dialog
    agent._dialog_history.append(dialog)

    decision = await agent.await_user_decision(dialog)

    assert decision.auto_resolved is False
    assert decision.action == "pivot_to_contradiction"
    report = agent.get_report(dialog.session_id)
    assert len(report.decisions) == 1
    assert report.decisions[0].action == "pivot_to_contradiction"


# ---------------------------------------------------------------------------
# Testes: get_report
# ---------------------------------------------------------------------------

def test_get_report_decisions_populated():
    agent = _make_agent()
    session_id = "sess_report"
    d = DialogDecision(dialog_id="d1", action="test_action")
    agent._record_decision(session_id, d)
    report = agent.get_report(session_id)
    assert len(report.decisions) == 1
    assert report.decisions[0].action == "test_action"
    assert report.total_decisions == 1


def test_get_report_empty_session():
    agent = _make_agent()
    report = agent.get_report("nonexistent_session")
    assert report.total_dialogs == 0
    assert report.total_decisions == 0


# ---------------------------------------------------------------------------
# Testes: _get_session_lock (serialização por sessão)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_lock_same_session_same_lock():
    agent = _make_agent()
    lock1 = agent._get_session_lock("sess_a")
    lock2 = agent._get_session_lock("sess_a")
    assert lock1 is lock2


@pytest.mark.asyncio
async def test_get_session_lock_different_sessions_different_locks():
    agent = _make_agent()
    lock_a = agent._get_session_lock("sess_a")
    lock_b = agent._get_session_lock("sess_b")
    assert lock_a is not lock_b


# ---------------------------------------------------------------------------
# Testes: create_alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_alert_returns_dialog_or_none():
    agent = _make_agent()
    result = await agent.create_alert("s1", "Sistema em risco!", urgency=0.99)
    # Com urgencia >= URGENCY_THRESHOLD_AUTO_INTERRUPT, deve retornar DialogTurn
    assert result is not None
    assert isinstance(result, DialogTurn)
    assert result.dialog_type == DialogType.ALERT_CRITICAL_FINDING


@pytest.mark.asyncio
async def test_create_alert_low_urgency_returns_none():
    agent = _make_agent()
    # urgency < URGENCY_THRESHOLD_AUTO_INTERRUPT
    result = await agent.create_alert("s1", "Aviso leve", urgency=0.5)
    assert result is None


# ---------------------------------------------------------------------------
# Testes: HITLDialogReport properties
# ---------------------------------------------------------------------------

def test_hitl_report_total_decisions_and_dialogs():
    dialog = _make_dialog()
    decision = DialogDecision(dialog_id="d1", action="test")
    report = HITLDialogReport(
        session_id="s1",
        dialogs=[dialog],
        decisions=[decision],
    )
    assert report.total_dialogs == 1
    assert report.total_decisions == 1


def test_hitl_report_user_engagement_rate_all_answered():
    dialogs = [_make_dialog() for _ in range(3)]
    # Nenhum foi timeout -> taxa = 1.0
    report = HITLDialogReport(session_id="s1", dialogs=dialogs)
    assert report.user_engagement_rate == 1.0


def test_hitl_report_user_engagement_rate_half_timeout():
    d1 = _make_dialog()
    d2 = _make_dialog()
    d2.was_timeout = True
    report = HITLDialogReport(session_id="s1", dialogs=[d1, d2])
    assert report.user_engagement_rate == 0.5
