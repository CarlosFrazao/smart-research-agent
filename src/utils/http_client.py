import aiohttp
import asyncio
import random
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]


class HTTPClient:
    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self._session

    def __del__(self):
        if hasattr(self, "_session") and self._session and not self._session.closed:
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._session.close())
            except RuntimeError:
                pass

    async def get(
        self,
        url: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self._request_with_retry("GET", url, headers=headers, params=params, **kwargs)

    async def post(
        self,
        url: str,
        headers: Optional[Dict] = None,
        json: Optional[Dict] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self._request_with_retry("POST", url, headers=headers, json_data=json, **kwargs)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        headers = headers or {}
        headers.setdefault("User-Agent", random.choice(USER_AGENTS))

        for attempt in range(self.max_retries):
            try:
                session = await self._get_session()
                if method.upper() == "GET":
                    req_ctx = session.get(url, headers=headers, params=params, **kwargs)
                elif method.upper() == "POST":
                    req_ctx = session.post(url, headers=headers, json=json_data, **kwargs)
                else:
                    raise ValueError(f"Método HTTP não suportado: {method}")

                async with req_ctx as resp:
                    resp.raise_for_status()
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return await resp.json()
                    return {"text": await resp.text(), "status": resp.status}

            except (asyncio.TimeoutError, aiohttp.ClientConnectorError) as e:
                logger.warning(
                    f"Erro temporário de conexão/timeout em {method} {url}: {type(e).__name__}({e}) "
                    f"(tentativa {attempt + 1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    raise
                wait_time = (2 ** attempt) + random.uniform(0.1, 1.0)
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.warning(
                    f"Erro genérico em {method} {url}: {type(e).__name__}({e}) "
                    f"(tentativa {attempt + 1}/{self.max_retries})"
                )
                if attempt == self.max_retries - 1:
                    raise
                wait_time = (2 ** attempt) + random.uniform(0.1, 1.0)
                await asyncio.sleep(wait_time)

        return {}

