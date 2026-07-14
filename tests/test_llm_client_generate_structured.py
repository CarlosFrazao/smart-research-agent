"""Testes de robustez de LLMClient.generate_structured (GAP 3 do PLANO_FECHAR_GAPS.md).

Hoje generate_structured() faz json.loads(response) SEM try/except
(src/clients/llm_client.py). Se o LLM devolver markdown/texto solto/vazio,
estoura json.JSONDecodeError — que no DeepResearcher silencia as hipóteses
reais ("Expecting value: line 1 column 1" do stress test).

Estes testes garantem que generate_structured NUNCA estoura para o caller:
extrai JSON de markdown com ruído, repara com 1 retry, e retorna fallback
seguro ([] para schema array, {} para schema object) em falha definitiva.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import json
import pytest

from src.clients.llm_client import LLMClient, OutputValidationError


def _client_with_generate(return_values):
    """LLMClient com self.generate mockado (sem rede/SDK)."""
    client = LLMClient.__new__(LLMClient)
    client.model = "test-model"
    client.token_economy = MagicMock()
    client.token_economy.count_tokens.return_value = 1
    client.token_economy.estimate_cost.return_value = (1, 0.0)
    client.max_repair_attempts = 1
    if isinstance(return_values, list):
        client.generate = AsyncMock(side_effect=return_values)
    else:
        client.generate = AsyncMock(return_value=return_values)
    return client


@pytest.mark.asyncio
async def test_parses_json_inside_markdown_fence():
    """JSON envolto em ```json ... ``` deve ser parseado, não estourar."""
    client = _client_with_generate('```json\n["a", "b"]\n```')
    result = await client.generate_structured(
        "prompt", {"type": "array", "items": {"type": "string"}}
    )
    assert result == ["a", "b"]


@pytest.mark.asyncio
async def test_parses_json_inside_prose_noise():
    """JSON no meio de texto solto deve ser extraído via regex."""
    client = _client_with_generate(
        'Claro, aqui está: ["x", "y"] — espero que ajude.'
    )
    result = await client.generate_structured(
        "prompt", {"type": "array", "items": {"type": "string"}}
    )
    assert result == ["x", "y"]


@pytest.mark.asyncio
async def test_empty_response_returns_safe_fallback_not_raises():
    """Resposta vazia não deve estourar JSONDecodeError; retorna [] para array."""
    client = _client_with_generate("")
    result = await client.generate_structured(
        "prompt", {"type": "array", "items": {"type": "string"}}
    )
    assert result == []


@pytest.mark.asyncio
async def test_object_schema_empty_response_returns_empty_dict():
    """Resposta vazia para schema object retorna {} (não estoura)."""
    client = _client_with_generate("")
    result = await client.generate_structured(
        "prompt", {"type": "object", "properties": {"k": {"type": "string"}}}
    )
    assert result == {}


@pytest.mark.asyncio
async def test_repairs_with_retry_on_garbage():
    """Lixo não-JSON na 1ª tentativa + JSON na 2ª (retry de reparo) -> parse ok."""
    client = _client_with_generate(
        [
            "Desculpe, não consegui formatar.",  # lixo
            '["ok"]',  # reparo
        ]
    )
    result = await client.generate_structured(
        "prompt", {"type": "array", "items": {"type": "string"}}
    )
    assert result == ["ok"]
    assert client.generate.await_count == 2


@pytest.mark.asyncio
async def test_pure_garbage_returns_fallback_after_retry():
    """Lixo nas duas tentativas -> retorna [] (array) sem estourar."""
    client = _client_with_generate(
        [
            "totalmente incompreensível",
            "ainda não consegui gerar JSON",
        ]
    )
    result = await client.generate_structured(
        "prompt", {"type": "array", "items": {"type": "string"}}
    )
    assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
