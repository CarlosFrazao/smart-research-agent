"""orchestrator_factory.py — Seleção do orquestrador conforme a configuração.

Ponto único de decisão entre o pipeline sequencial clássico (`Orchestrator`)
e o loop dinâmico ReAct (`ReActOrchestrator`). Antes desta factory, os quatro
pontos de entrada (`api/main.py`, `cli/main.py`, `src/mcp_server.py`) sempre
instanciavam `Orchestrator` diretamente, tornando a flag
`config.enable_dynamic_loop=True` inerte (§14.2 do PLANO_SRA_PARTE_3).

Uso:
    from src.orchestrator_factory import create_orchestrator

    orchestrator = create_orchestrator(config)
"""

from __future__ import annotations

from typing import Any

from src.orchestrator import Orchestrator
from src.utils.logging import setup_logger

logger = setup_logger("orchestrator-factory")

__all__ = ["create_orchestrator"]


def create_orchestrator(config: Any = None) -> Orchestrator:
    """Cria o orquestrador adequado ao modo configurado.

    Quando ``config.enable_dynamic_loop`` for ``True``, retorna um
    ``ReActOrchestrator`` (loop dinâmico ReAct); caso contrário, retorna o
    ``Orchestrator`` sequencial clássico. Como ``ReActOrchestrator`` é subclass
    de ``Orchestrator`` com a mesma assinatura de construtor, os chamadores não
    precisam distinguir o tipo concreto.

    O import de ``ReActOrchestrator`` é feito localmente para evitar import
    circular (``react_orchestrator`` importa ``Orchestrator``).

    Args:
        config: Instância de ``Config`` (ou compatível). Se ``None``, o
            ``Orchestrator`` aplica seus próprios defaults.

    Returns:
        Orchestrator: Instância de ``ReActOrchestrator`` ou ``Orchestrator``.
    """
    if getattr(config, "enable_dynamic_loop", False):
        from src.react_orchestrator import ReActOrchestrator

        logger.info(
            "create_orchestrator: usando ReActOrchestrator (loop dinâmico ativo)."
        )
        return ReActOrchestrator(config)

    logger.debug("create_orchestrator: usando Orchestrator sequencial clássico.")
    return Orchestrator(config)
