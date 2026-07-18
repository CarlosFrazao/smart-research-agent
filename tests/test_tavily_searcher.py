"""Testes TDD do TavilySearcher (FEAT-009).

Casos cobertos (task_fontes_pesquisa_hermes_sra.md — Bloco 2.1 / PRD 4.9.7):
1. TAVILY_API_KEY ausente -> factory nao instancia o searcher (requires_key).
2. POST /search mockado -> hit retorna SearchResult(source="tavily").
3. max_results > 20 -> payload envia max_results=20 (cap da API).
4. resultado sem url / failed_results -> descartado (normalize retorna None).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.search.registry import get_registry
from src.search.tavily_searcher import TavilySearcher, _TAVILY_MAX_RESULTS_CAP


def _make_searcher(monkeypatch, api_key: str = "tavily-test-key-123") -> TavilySearcher:
    """Helper: cria TavilySearcher com API key injetada no ambiente."""
    monkeypatch.setenv("TAVILY_API_KEY", api_key)  # pragma: allowlist secret
    monkeypatch.setenv("SRA_TAVILY_ENABLED", "true")
    return TavilySearcher({"max_results": 5})


def test_requires_key_blocks_factory_without_api_key(monkeypatch):
    """Sem TAVILY_API_KEY, o registro exige a key -> factory pulgaria o searcher."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    meta = get_registry().get("tavily")
    assert meta is not None, "TavilySearcher deve estar registrado via decorator"
    # O factory do SRA pula searchers cujo requires_key nao esta no ambiente.
    requires_key = meta.get("requires_key")
    assert requires_key == "TAVILY_API_KEY"
    assert not os.getenv(requires_key), "Pre-condicao: key ausente neste teste"
    would_instantiate = bool(os.getenv(requires_key))
    assert would_instantiate is False


def test_search_hit_returns_tavily_result(monkeypatch):
    """POST /search mockado retorna resultados -> SearchResult(source='tavily')."""
    searcher = _make_searcher(monkeypatch)

    result = {
        "title": "Tavily Search API",
        "url": "https://docs.tavily.com/search",
        "content": "Tavily is a search API optimized for LLMs and RAG pipelines.",
    }
    response = MagicMock()
    response.json.return_value = {"results": [result]}
    searcher._http_request = AsyncMock(return_value=response)

    results = _run(searcher.search("tavily search api"))

    assert len(results) == 1
    assert results[0].source == "tavily"
    assert results[0].url == "https://docs.tavily.com/search"
    assert results[0].title == "Tavily Search API"
    assert "LLMs" in results[0].description
    # description truncado em 300 chars.
    assert len(results[0].description) <= 300
    searcher._http_request.assert_awaited_once()


def test_search_caps_max_results_at_20(monkeypatch):
    """max_results > 20 do config e limitado ao teto da API (20) no payload."""
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key-123")  # pragma: allowlist secret
    searcher = TavilySearcher({"max_results": 50})

    response = MagicMock()
    response.json.return_value = {"results": []}
    captured = {}

    async def _fake_request(method, url, *, headers=None, params=None, json_body=None):
        captured["json_body"] = json_body
        return response

    searcher._http_request = _fake_request

    _run(searcher.search("large query"))

    assert captured["json_body"]["max_results"] == _TAVILY_MAX_RESULTS_CAP
    assert _TAVILY_MAX_RESULTS_CAP == 20


def test_search_discards_results_without_url(monkeypatch):
    """Itens sem url (ex.: failed_results) sao descartados pelo normalize."""
    searcher = _make_searcher(monkeypatch)

    response = MagicMock()
    response.json.return_value = {
        "results": [
            {"title": "Valido", "url": "https://ok.example/a", "content": "conteudo"},
            # falha de extracao: sem url -> deve ser descartado.
            {"title": "Sem URL", "content": "ignorado"},
            # entrada de failed_results do /extract: sem url.
            {"url": "", "error": "extraction failed"},
        ]
    }
    searcher._http_request = AsyncMock(return_value=response)

    results = _run(searcher.search("query"))

    assert len(results) == 1
    assert results[0].url == "https://ok.example/a"


def test_search_api_error_returns_empty(monkeypatch):
    """Erro de API (qualquer excecao) -> fallback() retorna lista vazia."""
    searcher = _make_searcher(monkeypatch)
    searcher._http_request = AsyncMock(side_effect=RuntimeError("Tavily 500"))

    results = _run(searcher.search("anything"))

    assert results == []
    searcher._http_request.assert_awaited_once()


def test_normalize_without_url_returns_none():
    """normalize de dict sem url retorna None (descartavel)."""
    searcher = TavilySearcher({"max_results": 5})
    assert searcher.normalize({"title": "x", "content": "y"}) is None
    assert searcher.normalize({"url": "", "content": "z"}) is None


def test_normalize_with_empty_content_yields_empty_description():
    """content ausente gera description vazia (robustez)."""
    searcher = TavilySearcher({"max_results": 5})
    sr = searcher.normalize({"title": "T", "url": "https://e.x/1", "content": ""})
    assert sr is not None
    assert sr.description == ""


# ── Helper de execucao sincrona para os testes (o search() e async) ──


def _run(coro):
    """Executa uma corrotina criando um novo event loop (sem deprecation)."""
    import asyncio

    return asyncio.run(coro)
