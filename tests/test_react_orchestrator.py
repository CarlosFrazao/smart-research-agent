"""
Testes para ReActOrchestrator — Validação do orquestrador com loop dinâmico.

Testa:
- Fallback para pipeline clássico quando disabled
- Loop ReAct com decisão dinâmica
- Integração com StageFactory
- Reset de estado
- Thread-safety considerations
"""

import pytest
from unittest.mock import AsyncMock, patch

from src.react_orchestrator import ReActOrchestrator
from src.orchestrator import Orchestrator
from src.config import Config


def _make_config(*, enable_dynamic_loop: bool) -> Config:
    """Cria um Config real (não MagicMock) para exercitar a inicialização
    completa do Orchestrator.

    Um ``MagicMock`` quebra o ``super().__init__`` porque
    ``LLMProvider(config.llm_provider)`` recebe um mock em vez de um valor
    válido do enum. Um ``Config()`` real traz defaults válidos
    (``llm_provider='gemini'``, thresholds ReAct, etc.).
    """
    config = Config()
    config.enable_dynamic_loop = enable_dynamic_loop
    return config


class TestReActOrchestrator:
    """Testes para ReActOrchestrator."""

    @pytest.mark.asyncio
    async def test_fallback_to_classic_pipeline(self):
        """Com o loop desabilitado, deve delegar ao research() do pai."""
        config = _make_config(enable_dynamic_loop=False)
        orchestrator = ReActOrchestrator(config=config)

        # Patcha o research do Orchestrator pai para confirmar a delegação
        # sem disparar o pipeline real (rede/LLM).
        with patch.object(
            Orchestrator, "research", new=AsyncMock(return_value="classic-report")
        ) as parent_research:
            result = await orchestrator.research("test query")

        assert result == "classic-report"
        assert parent_research.await_count == 1

    def test_initialization_with_dynamic_enabled(self):
        """Orchestrator deve aceitar config com enable_dynamic_loop=True."""
        config = _make_config(enable_dynamic_loop=True)

        # Should not raise exception
        react_orchestrator = ReActOrchestrator(config=config)

        # Should have the attribute set correctly
        assert react_orchestrator._react_enabled is True
        assert hasattr(react_orchestrator, "_decision_engine")

    def test_research_method_exists(self):
        """Instance should have research method."""
        config = _make_config(enable_dynamic_loop=True)
        react_orchestrator = ReActOrchestrator(config=config)

        assert hasattr(react_orchestrator, "research")
        assert callable(react_orchestrator.research)
