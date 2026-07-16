"""Testes TDD para FEAT-002 (Resiliência SRA, Bloco 2).

Verifica que `LLMClient.generate_structured` sinaliza falha de forma
visível (via `last_failure`) quando o LLM retorna resposta vazia/inválida,
retornando o fallback seguro (`[]`/`{}`) sem estourar exceção para o caller.

Backend-only — sem UI. Testes determinísticos com mock do provider.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.clients.llm_client import LLMClient, LLMProvider


def _make_client() -> LLMClient:
    """Constrói um LLMClient mínimo para teste (sem _init_client real)."""
    client = LLMClient.__new__(LLMClient)
    client.provider = LLMProvider.OPENROUTER
    client.config = {}
    client.model = "test-model"
    client.model_router = None
    client._api_keys = []
    client._models = []
    client._fallback_configs = {}
    client.max_repair_attempts = 1
    client.last_failure: str | None = None
    return client


async def test_last_failure_set_when_llm_returns_empty() -> None:
    """Mock generate() -> '' deve setar last_failure e retornar fallback []."""
    client = _make_client()
    client.generate = AsyncMock(return_value="")  # resposta vazia

    result = await client.generate_structured(
        "extraia itens", {"type": "array", "items": {"type": "string"}}
    )

    assert result == []  # fallback seguro para array
    assert client.last_failure is not None
    assert "vazio" in client.last_failure.lower() or "json" in client.last_failure.lower()


async def test_last_failure_set_when_llm_returns_non_json() -> None:
    """Mock generate() -> texto sem JSON deve setar last_failure e retornar {}."""
    client = _make_client()
    client.generate = AsyncMock(return_value="desculpe, não entendi")

    result = await client.generate_structured(
        "extraia objeto", {"type": "object", "properties": {"x": {"type": "string"}}}
    )

    assert result == {}  # fallback seguro para object
    assert client.last_failure is not None


async def test_last_failure_none_on_success() -> None:
    """Quando o LLM retorna JSON válido, last_failure permanece None."""
    client = _make_client()
    client.generate = AsyncMock(return_value='["a", "b", "c"]')

    result = await client.generate_structured(
        "extraia itens", {"type": "array", "items": {"type": "string"}}
    )

    assert result == ["a", "b", "c"]
    assert client.last_failure is None


async def test_last_failure_overwritten_across_calls() -> None:
    """Uma chamada bem-sucedida limpa o last_failure de uma falha anterior."""
    client = _make_client()
    client.generate = AsyncMock(return_value="")

    await client.generate_structured("q", {"type": "array", "items": {"type": "string"}})
    assert client.last_failure is not None

    client.generate = AsyncMock(return_value='{"ok": true}')
    result = await client.generate_structured(
        "q", {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    )
    assert result == {"ok": True}
    assert client.last_failure is None


def test_structured_generation_error_type_exists() -> None:
    """A exceção StructuredGenerationError deve existir e ser RuntimeError."""
    from src.clients.llm_client import StructuredGenerationError

    assert issubclass(StructuredGenerationError, RuntimeError)
    exc: Exception = StructuredGenerationError("falha")
    assert isinstance(exc, RuntimeError)
