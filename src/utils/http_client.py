from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.waf_specialist_agent import WAFSpecialistAgent

import aiohttp

from src.utils.rate_limiter import DomainRateLimiter

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]


class HTTPClient:
    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        waf_agent: WAFSpecialistAgent | None = None,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None

        # Inicializa o WAF Specialist Agent de evasão de bloqueio
        if waf_agent is not None:
            self.waf_agent = waf_agent
        else:
            redis_client = None
            redis_url = os.environ.get("REDIS_URL") or os.environ.get(
                "CELERY_BROKER_URL"
            )
            if redis_url:
                try:
                    import redis.asyncio as aioredis

                    redis_client = aioredis.from_url(redis_url, decode_responses=True)
                except Exception as e:
                    logger.debug(
                        f"Não foi possível conectar ao Redis para o WAF Specialist Agent: {e}"
                    )

            from src.waf_specialist_agent import WAFSpecialistAgent

            self.waf_agent = WAFSpecialistAgent(redis_client=redis_client)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, connector=connector
            )
        return self._session

    def __del__(self):
        # Best-effort: se o dono esqueceu de chamar ``close()``, tenta agendar
        # o fechamento da sessão aiohttp enquanto o loop ainda estiver ativo.
        # O fechamento definitivo é responsabilidade de ``close()`` (chamado
        # por BaseSearcher.close() / Orchestrator.close_searchers()).
        session = getattr(self, "_session", None)
        if session is None or session.closed:
            return
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(session.close())
        except RuntimeError:
            pass

    async def get(
        self,
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        return await self._request_with_retry(
            "GET", url, headers=headers, params=params, **kwargs
        )

    async def post(
        self,
        url: str,
        headers: dict | None = None,
        json: dict | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        return await self._request_with_retry(
            "POST", url, headers=headers, json_data=json, **kwargs
        )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        json_data: dict | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        headers = headers or {}

        # Injeta headers stealth do WAF Specialist Agent se ativo
        if self.waf_agent:
            stealth_headers = self.waf_agent.get_stealth_headers(url)
            headers = {**stealth_headers, **headers}
        else:
            headers.setdefault("User-Agent", random.choice(USER_AGENTS))

        # Throttling por domínio — respeita limites de cada API
        await DomainRateLimiter.wait(url)

        for attempt in range(self.max_retries):
            try:
                session = await self._get_session()
                if method.upper() == "GET":
                    req_ctx = session.get(url, headers=headers, params=params, **kwargs)
                elif method.upper() == "POST":
                    req_ctx = session.post(
                        url, headers=headers, json=json_data, **kwargs
                    )
                else:
                    raise ValueError(f"Método HTTP não suportado: {method}")

                async with req_ctx as resp:
                    # Alimenta o rate limiter adaptativo com o status real da resposta
                    DomainRateLimiter.record(url, resp.status)

                    text = await resp.text()

                    # Intercepta e inspeciona se há sinal de bloqueio de WAF
                    if self.waf_agent:
                        signal = self.waf_agent.inspect_response(url, resp.status, text)
                        if signal:
                            # Tenta aplicar contramedida assincronamente (backoff, proxy, etc.)
                            cm = await self.waf_agent.apply_countermeasure(signal)
                            if cm.get("success"):
                                kwargs = self.waf_agent.apply_countermeasure_to_request(
                                    cm, kwargs
                                )
                                if cm.get("new_headers"):
                                    headers = cm["new_headers"]
                                logger.info(
                                    f"[HTTPClient] Contramedida aplicada com sucesso para {url}. "
                                    f"Fazendo retry ({attempt + 1}/{self.max_retries})."
                                )
                                continue  # Executa retry imediatamente com novos parâmetros

                    resp.raise_for_status()
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return await resp.json()
                    return {"text": text, "status": resp.status}

            except aiohttp.ClientResponseError as e:
                if e.status in {400, 401, 403, 404, 405}:
                    logger.debug(
                        f"Erro HTTP permanente {e.status} em {method} {url} — abortando retries."
                    )
                    raise
                logger.warning(
                    f"Erro de resposta HTTP {e.status} em {method} {url}: {e} "
                    f"(tentativa {attempt + 1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0.1, 1.0)
                await asyncio.sleep(wait_time)
            except (TimeoutError, aiohttp.ClientConnectorError) as e:
                logger.warning(
                    f"Erro temporário de conexão/timeout em {method} {url}: {type(e).__name__}({e}) "
                    f"(tentativa {attempt + 1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0.1, 1.0)
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.warning(
                    f"Erro genérico em {method} {url}: {type(e).__name__}({e}) "
                    f"(tentativa {attempt + 1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0.1, 1.0)
                await asyncio.sleep(wait_time)

        return {}

    async def close(self) -> None:
        """Fecha a ClientSession aiohttp de forma assíncrona e limpa."""
        if self._session and not self._session.closed:
            await self._session.close()
