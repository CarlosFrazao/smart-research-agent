"""Teste de integração: DeepResearcher não degrada nem estoura com LLM não-JSON.

Cobre o GAP 3 do PLANO_FECHAR_GAPS.md: o stress test reportou
"Hypothesis generation failed: Expecting value: line 1 column 1" em
`--mode deep` porque LLMClient.generate_structured estourava em JSON inválido.

Agora _generate_hypotheses deve retornar a lista de fallback (sem exceção),
garantindo que o deep mode não quebra silenciosamente.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.deep_researcher import DeepResearcher
from src.types import SearchResult


def _make_researcher(llm_returns: list[str]):
    """DeepResearcher com LLM mockado (generate_structured retorna lixo)."""
    llm = MagicMock()
    # Simula o LLM devolvendo texto não-JSON (cenário do stress test)
    llm.generate_structured = AsyncMock(side_effect=llm_returns)
    llm.token_economy = MagicMock()

    researcher = DeepResearcher.__new__(DeepResearcher)
    researcher.llm = llm
    researcher.orchestrator = None
    researcher.memory = None
    researcher.config = None
    researcher.MAX_BRANCHES = 4
    return researcher


@pytest.mark.asyncio
async def test_hypotheses_fallback_when_llm_returns_garbage():
    """LLM retorna não-JSON -> _generate_hypotheses retorna lista (fallback), sem exceção."""
    researcher = _make_researcher(
        ["I cannot produce JSON right now.", "still not json"]
    )
    results = [
        SearchResult(source="web", title="t", url="http://t", description="d", metrics={})
    ]
    # Não deve estourar; deve retornar a lista de fallback de 4 hipóteses
    hypotheses = await researcher._generate_hypotheses("quantum computing", results)
    assert isinstance(hypotheses, list)
    assert len(hypotheses) == 4
    assert all(isinstance(h, str) and h for h in hypotheses)


@pytest.mark.asyncio
async def test_hypotheses_fallback_on_json_decode_error():
    """LLM levanta JSONDecodeError -> lista de fallback, sem propagar."""
    import json

    researcher = _make_researcher(
        [json.JSONDecodeError("Expecting value", "", 0)]
    )
    results = []
    hypotheses = await researcher._generate_hypotheses("test query", results)
    assert len(hypotheses) == 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
