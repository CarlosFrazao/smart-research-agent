"""Testa que BaseSearcher.close() encerra a aiohttp.ClientSession do HTTPClient.

Regressão para o vazamento de 'Unclosed client session' observado em produção:
searchers que usam ``HTTPClient`` (aiohttp) não fechavam a sessão porque
``BaseSearcher.close()`` só fechava ``self._client`` (httpx).
"""

from __future__ import annotations

import asyncio

import pytest

from src.search.base_searcher import BaseSearcher
from src.utils.http_client import HTTPClient


class _StubSearcher(BaseSearcher):
    """Searcher concreto mínimo que instancia um HTTPClient aiohttp."""

    async def search(self, query: str, **kwargs):  # pragma: no cover - não usado
        return []

    def normalize(self, raw):  # pragma: no cover - não usado
        return []

    def setup_http(self) -> None:
        self.http = HTTPClient(timeout=5)


def test_close_closes_aiohttp_session_without_error():
    searcher = _StubSearcher({"name": "stub", "timeout": 5})
    searcher.setup_http()
    # Garante que a sessão interna existe e está aberta.
    session = asyncio.get_event_loop().run_until_complete(searcher.http._get_session())
    assert session is not None
    assert not session.closed

    asyncio.get_event_loop().run_until_complete(searcher.close())

    # Após close(), a sessão aiohttp deve estar fechada.
    assert session.closed is True
    assert searcher.http._session is None or searcher.http._session.closed


def test_close_is_safe_when_no_http_present():
    searcher = _StubSearcher({"name": "stub", "timeout": 5})
    # Não chama setup_http -> não há self.http
    asyncio.get_event_loop().run_until_complete(searcher.close())
    assert True  # não deve levantar
