"""Testes de fail-fast do FirecrawlClient em ConnectionRefused (F3 / B1).

Cobre o B1 do plano de blindagem black_ops: quando o container Firecrawl está
off (WinError 10061 / NewConnectionError / ConnectionRefusedError), o cliente
deve fazer NO MÁXIMO 1 retry curto (1s) e desistir — NÃO entrar na cascata de
3 tentativas de 7s que gerava 21+ retries no teste de estresse.

O teste conta as chamadas reais ao coroutine alvo para provar que o fail-fast
está em vigor (sem rede, sem container real).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.clients.firecrawl_client import FirecrawlClient


def _make_client() -> FirecrawlClient:
    """Instancia FirecrawlClient sem depender do SDK real (api_key vazia)."""
    return FirecrawlClient(api_key="")


@pytest.mark.asyncio
async def test_connection_refused_fails_fast_max_two_calls():
    """ConnectionRefusedError (WinError 10061) → ≤2 chamadas + propaga erro."""
    client = _make_client()
    calls = 0

    async def _boom():
        nonlocal calls
        calls += 1
        # Simula httpx.NewConnectionError envolvendo ConnectionRefusedError
        # (causa raiz [WinError 10061] quando o container está offline).
        cause = ConnectionRefusedError("[WinError 10061] Connect call failed")
        raise ConnectionRefusedError("[WinError 10061]") from cause

    with pytest.raises(ConnectionRefusedError):
        await client._with_retry(_boom)

    # Fail-fast: 1 tentativa inicial + 1 retry curto = 2 chamadas (não 3 da
    # cascata padrão de 7s).
    assert calls <= 2, f"fail-fast deve limitar a 2 chamadas, foram {calls}"


@pytest.mark.asyncio
async def test_connection_refused_via_newconnectionerror_cause():
    """NewConnectionError com __cause__ ConnectionRefusedError também fail-fast."""
    client = _make_client()
    calls = 0

    class FakeNewConnectionError(Exception):
        pass

    async def _boom():
        nonlocal calls
        calls += 1
        exc = FakeNewConnectionError("connection failed")
        exc.__cause__ = ConnectionRefusedError("[WinError 10061]")
        raise exc

    with pytest.raises(Exception):
        await client._with_retry(_boom)

    assert calls <= 2, f"fail-fast deve limitar a 2 chamadas, foram {calls}"


@pytest.mark.asyncio
async def test_is_connection_refused_detects_variants():
    """_is_connection_refused reconhece as variantes de recusa de conexão."""
    client = _make_client()
    # WinError 10061 direto
    assert client._is_connection_refused(
        ConnectionRefusedError("[WinError 10061] Connect call failed")
    )
    # "connection refused" literal
    assert client._is_connection_refused(RuntimeError("connection refused"))
    # ConnectionRefusedError nativo
    assert client._is_connection_refused(ConnectionRefusedError("refused"))
    # Não deve classificar 503/timeout como recusa de conexão
    assert not client._is_connection_refused(RuntimeError("503 Service Unavailable"))
    assert not client._is_connection_refused(RuntimeError("timeout"))


@pytest.mark.asyncio
async def test_standard_retryable_still_retries_three_times():
    """Erro retryable comum (503) mantém a cascata de 3 tentativas (não B1)."""
    client = _make_client()
    calls = 0

    async def _boom():
        nonlocal calls
        calls += 1
        raise RuntimeError("503 Service Unavailable")

    with pytest.raises(RuntimeError):
        await client._with_retry(_boom)

    # Cascata padrão de 3 tentativas preservada para erros transitórios.
    assert calls == 3, f"erro retryable deve fazer 3 tentativas, foram {calls}"


@pytest.mark.asyncio
async def test_success_returns_without_retry():
    """Sucesso na 1ª chamada não dispara retry."""
    client = _make_client()
    calls = 0

    async def _ok():
        nonlocal calls
        calls += 1
        return "resultado"

    result = await client._with_retry(_ok)
    assert result == "resultado"
    assert calls == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
