"""Testes de resiliência do FirecrawlSearcher frente a erro de autenticação (401).

Cobre o GAP 1 do PLANO_FECHAR_GAPS.md: quando o token do Firecrawl é inválido
(ex.: 401 Unauthorized), o FirecrawlSearcher deve cascatear para o web_fallback
(searcher "web") em vez de retornar lista vazia silenciosamente.

Segue o padrão já consagrado em pubmed_searcher.py / youtube_searcher.py
(web_fallback -> WebSearcher).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.search.base_searcher import BaseSearcher
from src.search.firecrawl_searcher import FirecrawlSearcher
from src.types import SearchResult


def _make_searcher(auth_failed: bool, search_side_effect=None, search_return=None):
    """Instancia FirecrawlSearcher com um client mockado (sem rede)."""
    searcher = FirecrawlSearcher({"timeout": 30, "max_results": 5})
    client = MagicMock()
    client.auth_failed = auth_failed
    if search_side_effect is not None:
        client.search = AsyncMock(side_effect=search_side_effect)
    else:
        client.search = AsyncMock(return_value=search_return or [])
    # Desliga o circuit breaker/herança que possa tocar rede
    searcher.circuit_breaker = MagicMock()
    searcher.circuit_breaker.call = lambda fn, *a, **k: fn(*a, **k)
    searcher.client = client
    return searcher


def _fake_web_fallback(results: list[SearchResult]):
    fb = MagicMock(spec=BaseSearcher)
    fb.enabled = True
    fb.search = AsyncMock(return_value=results)
    return fb


@pytest.mark.asyncio
async def test_auth_error_delegates_to_web_fallback():
    """401 (auth_failed) deve delegar ao web_fallback, não retornar []."""
    fallback_results = [SearchResult(source="web", title="x", url="http://x", description="", metrics={})]
    searcher = _make_searcher(
        auth_failed=True, search_side_effect=RuntimeError("401 Unauthorized")
    )
    searcher.web_fallback = _fake_web_fallback(fallback_results)

    result = await searcher.search("termo de busca")

    assert result == fallback_results
    searcher.web_fallback.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_error_empty_result_triggers_fallback():
    """auth_failed + resultado vazio do client deve acionar fallback."""
    fallback_results = [SearchResult(source="web", title="y", url="http://y", description="", metrics={})]
    searcher = _make_searcher(auth_failed=True, search_return=[])
    searcher.web_fallback = _fake_web_fallback(fallback_results)

    result = await searcher.search("outra query")

    assert result == fallback_results


@pytest.mark.asyncio
async def test_no_fallback_returns_empty_when_web_fallback_none():
    """Sem web_fallback, preserva comportamento antigo (lista vazia)."""
    searcher = _make_searcher(
        auth_failed=True, search_side_effect=RuntimeError("401 Unauthorized")
    )
    searcher.web_fallback = None

    result = await searcher.search("query qualquer")

    assert result == []


@pytest.mark.asyncio
async def test_normal_results_returned_when_no_auth_error():
    """Sem auth error, retorna resultados normalizados do Firecrawl."""
    raw = [{"title": "T", "url": "http://t", "markdown": "conteudo"}]
    searcher = _make_searcher(auth_failed=False, search_return=raw)
    searcher.web_fallback = _fake_web_fallback([])

    result = await searcher.search("query normal")

    assert len(result) == 1
    assert result[0].source == "firecrawl"
    assert result[0].title == "T"
    searcher.web_fallback.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_detects_401_as_auth_error():
    """FirecrawlClient._is_auth_error reconhece 401 / unauthorized / api key."""
    from src.clients.firecrawl_client import FirecrawlClient

    client = FirecrawlClient(api_key="invalid")
    assert client._is_auth_error(RuntimeError("401 Unauthorized"))
    assert client._is_auth_error(RuntimeError("Authentication failed"))
    assert client._is_auth_error(RuntimeError("Invalid API key provided"))
    assert not client._is_auth_error(RuntimeError("503 Service Unavailable"))
    assert not client._is_auth_error(RuntimeError("timeout"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
