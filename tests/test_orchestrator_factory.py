"""Testes da fábrica de orquestrador (§14.2 — Plano Parte 3 Fase 1).

Garante que `create_orchestrator` seleciona `ReActOrchestrator` quando
`enable_dynamic_loop=True` e `Orchestrator` caso contrário, sem instanciar
as classes reais (evita o wiring pesado de dependências).
"""

from unittest.mock import MagicMock, patch

from src.orchestrator_factory import create_orchestrator


def test_create_orchestrator_classic_when_dynamic_loop_disabled():
    """enable_dynamic_loop=False (default) deve retornar o Orchestrator clássico."""
    config = MagicMock()
    config.enable_dynamic_loop = False

    with patch("src.orchestrator_factory.Orchestrator") as mock_orch:
        result = create_orchestrator(config)

    mock_orch.assert_called_once_with(config)
    assert result is mock_orch.return_value


def test_create_orchestrator_react_when_dynamic_loop_enabled():
    """enable_dynamic_loop=True deve retornar o ReActOrchestrator."""
    config = MagicMock()
    config.enable_dynamic_loop = True

    with patch("src.react_orchestrator.ReActOrchestrator") as mock_react:
        result = create_orchestrator(config)

    mock_react.assert_called_once_with(config)
    assert result is mock_react.return_value


def test_create_orchestrator_defaults_to_classic_without_flag():
    """Config sem o atributo enable_dynamic_loop deve usar o Orchestrator clássico."""

    class _CfgSemFlag:
        pass

    with patch("src.orchestrator_factory.Orchestrator") as mock_orch:
        create_orchestrator(_CfgSemFlag())

    mock_orch.assert_called_once()
