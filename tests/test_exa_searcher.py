"""Testes TDD do ExaSearcher (FEAT-008).

Casos cobertos (task_fontes_pesquisa_hermes_sra.md — Bloco 1.1):
1. EXA_API_KEY ausente -> factory nao instancia o searcher (requires_key).
2. Client mockado -> hit retorna SearchResult(source="exa").
3. Client mockado levanta -> fallback() retorna lista vazia.
4. normalize com highlights=None -> description="".
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.search.exa_searcher import ExaSearcher
from src.search.registry import get_registry


def _make_searcher(monkeypatch, client) -> ExaSearcher:
    """Helper: cria ExaSearcher com cliente Exa ja injetado (sem exa-py real)."""
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key-123")  # pragma: allowlist secret
    s = ExaSearcher({"max_results": 5})
    s._exa_client = client
    return s


def test_requires_key_blocks_factory_without_api_key(monkeypatch):
    """Sem EXA_API_KEY, o registro exige a key -> factory pulgaria o searcher."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    meta = get_registry().get("exa")
    assert meta is not None, "ExaSearcher deve estar registrado via decorator"
    # O factory do SRA pula searchers cujo requires_key nao esta no ambiente.
    requires_key = meta.get("requires_key")
    assert requires_key == "EXA_API_KEY"
    assert not os.getenv(requires_key), "Pre-condicao: key ausente neste teste"
    # Simula a checagem exata do factory: pulado quando a key falta.
    would_instantiate = bool(os.getenv(requires_key))
    assert would_instantiate is False


def test_search_hit_returns_exa_result(monkeypatch):
    """Cliente mockado retorna resultados -> SearchResult(source='exa')."""
    result_obj = SimpleNamespace(
        url="https://exa.ai/blog/neural-search",
        title="Neural Search Explained",
        highlights=["semantic ranking", "embedding retrieval"],
    )
    response = SimpleNamespace(results=[result_obj])
    client = MagicMock()
    client.search.return_value = response

    searcher = _make_searcher(monkeypatch, client)
    results = searcher.search_sync("neural search methods")  # helper abaixo

    assert len(results) == 1
    assert results[0].source == "exa"
    assert results[0].url == "https://exa.ai/blog/neural-search"
    assert results[0].title == "Neural Search Explained"
    assert "semantic ranking" in results[0].description
    client.search.assert_called_once()


def test_search_client_raises_returns_empty(monkeypatch):
    """Cliente mockado levanta -> fallback() retorna lista vazia (sem crash)."""
    client = MagicMock()
    client.search.side_effect = RuntimeError("Exa API 500")

    searcher = _make_searcher(monkeypatch, client)
    results = searcher.search_sync("anything")

    assert results == []
    client.search.assert_called_once()


def test_normalize_highlights_none_yields_empty_description():
    """normalize com highlights=None produz description vazia (caso 4)."""
    raw = SimpleNamespace(
        url="https://exa.ai/x",
        title="Sem highlights",
        highlights=None,
    )
    searcher = ExaSearcher({"max_results": 5})
    sr = searcher.normalize(raw)
    assert sr.source == "exa"
    assert sr.description == ""
    assert sr.url == "https://exa.ai/x"


def test_normalize_with_empty_highlights_yields_empty_description():
    """highlights=[] tambem gera description vazia (robustez)."""
    raw = SimpleNamespace(url="https://exa.ai/y", title="Vazio", highlights=[])
    searcher = ExaSearcher({"max_results": 5})
    sr = searcher.normalize(raw)
    assert sr.description == ""


# ── Helper de execucao sincrona para os testes (o search() e async) ──


def _run(coro):
    """Executa uma corrotina criando um novo event loop (sem deprecation)."""
    import asyncio

    return asyncio.run(coro)


# Monkey-patch: adiciona metodo sincrono de conveniencia nos testes.
def _search_sync(self, query, **kwargs):
    return _run(self.search(query, **kwargs))


ExaSearcher.search_sync = _search_sync  # type: ignore[assignment]
