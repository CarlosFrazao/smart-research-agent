"""Testa que AwesomeSearcher.close() encerra o GitHubSearcher interno.

Regressão para o vazamento da 1 aiohttp.ClientSession restante: o
AwesomeSearcher instancia um ``GitHubSearcher`` próprio (``self.github``)
cujo ``HTTPClient`` não está no dict de searchers do orquestrador. Sem
fechar explicitamente, sua sessão aiohttp vazava ("Unclosed client session").
"""

from __future__ import annotations

import asyncio

from src.search.awesome_searcher import AwesomeSearcher


def test_awesome_close_closes_internal_github_session():
    searcher = AwesomeSearcher({"name": "awesome", "timeout": 5})
    # O GitHubSearcher interno criou sua própria sessão aiohttp ao buscar.
    internal = searcher.github
    session = asyncio.get_event_loop().run_until_complete(internal.http._get_session())
    assert not session.closed

    asyncio.get_event_loop().run_until_complete(searcher.close())

    # Tanto o http próprio do Awesome quanto o do GitHubSearcher interno fecham.
    assert internal.http._session is None or internal.http._session.closed
    assert searcher.http._session is None or searcher.http._session.closed


def test_awesome_close_is_safe_without_internal_github():
    # Caso de borda: se self.github não existir, não deve levantar.
    searcher = AwesomeSearcher({"name": "awesome", "timeout": 5})
    del searcher.github
    asyncio.get_event_loop().run_until_complete(searcher.close())
    assert True
