"""
Cliente HTTP compartilhado.

Problema original: cada searcher criava seu próprio `httpx.Client` (ou
`requests.Session`), cada um com timeout diferente, sem pool de conexões
compartilhado (18 pools TCP em vez de 1), sem política de retry consistente
e sem um `User-Agent` padronizado (alguns searchers eram bloqueados por
esse motivo).

Solução: `SharedHTTPClient` é um singleton assíncrono (um único
`httpx.AsyncClient`, reaproveitado por todos os searchers) com retry/backoff
exponencial embutido para status 429/5xx.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from .exceptions import UpstreamHTTPError

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
DEFAULT_USER_AGENT = (
    "smart-research-agent/1.0 (+https://github.com/CarlosFrazao/smart-research-agent)"
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SharedHTTPClient:
    """
    Singleton assíncrono em torno de httpx.AsyncClient.

    Uso:
        client = await SharedHTTPClient.get_instance()
        response = await client.request("github", "GET", url, params=params)
    """

    _instance: "SharedHTTPClient | None" = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    @classmethod
    async def get_instance(cls) -> "SharedHTTPClient":
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    async def aclose(cls) -> None:
        async with cls._lock:
            if cls._instance is not None:
                await cls._instance._client.aclose()
                cls._instance = None

    async def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(
                    method, url, params=params, headers=headers, json=json_body
                )
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    if response.status_code >= 400:
                        raise UpstreamHTTPError(source, response.status_code, url)
                    return response
                last_exc = UpstreamHTTPError(source, response.status_code, url)

            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2**attempt))

        assert last_exc is not None
        raise last_exc
