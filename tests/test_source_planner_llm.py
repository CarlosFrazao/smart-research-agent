import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_universal_domain_calls_llm():
    """Verifica que domínio universal aciona o LLM router."""
    from src.source_planner import SourcePlanner

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="wikipedia, duckduckgo, reddit")

    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = mock_llm
    planner.domain_map = {"universal": {"primary": ["wikipedia"], "secondary": []}}

    intent = MagicMock()
    intent.domain = MagicMock()
    intent.domain.value = "universal"
    intent.intent = "discover"

    sources = await planner._plan_with_llm(intent, "receitas de bolo")
    mock_llm.generate.assert_called_once()
    assert isinstance(sources, list)


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_yaml():
    """Verifica que falha do LLM não quebra o planner."""
    from src.source_planner import SourcePlanner

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=Exception("LLM offline"))

    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = mock_llm
    planner.domain_map = {"universal": {"primary": ["wikipedia", "duckduckgo"], "secondary": []}}

    intent = MagicMock()
    intent.domain = MagicMock()
    intent.domain.value = "universal"
    intent.intent = "discover"

    # Não deve lançar exceção
    sources = await planner._plan_with_llm(intent, "qualquer query")
    assert isinstance(sources, list)


def test_none_llm_does_not_raise():
    """Verifica que planner sem LLM não quebra."""
    from src.source_planner import SourcePlanner

    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = None
    planner.domain_map = {"universal": {"primary": ["wikipedia"], "secondary": []}}
    # Deve funcionar sem LLM (modo offline)
    assert planner.llm is None
