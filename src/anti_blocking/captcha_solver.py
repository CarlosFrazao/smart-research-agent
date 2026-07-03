"""
Integração com serviços de CAPTCHA solving.
Suporta: 2captcha, anticaptcha, capsolver.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class CaptchaSolver:
    POLL_INTERVAL = 5  # segundos entre polls
    MAX_POLLS = 30  # 150s timeout total

    def __init__(self, provider: str, api_key: str):
        self.provider = provider
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30)

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str | None:
        if not self.api_key:
            logger.error("CAPTCHA API key não configurada.")
            return None
        if self.provider == "2captcha":
            return await self._solve_2captcha(site_key, page_url)
        elif self.provider == "capsolver":
            return await self._solve_capsolver(site_key, page_url)
        logger.warning(f"Provider de CAPTCHA '{self.provider}' não suportado")
        return None

    async def _solve_2captcha(self, site_key: str, page_url: str) -> str | None:
        try:
            r = await self.client.post(
                "https://2captcha.com/in.php",
                data={
                    "key": self.api_key,
                    "method": "userrecaptcha",
                    "googlekey": site_key,
                    "pageurl": page_url,
                    "json": 1,
                },
            )
            task_id = r.json().get("request")
            if not task_id:
                logger.error(f"2captcha rejeitou o task: {r.text}")
                return None

            # Polling
            for _ in range(self.MAX_POLLS):
                await asyncio.sleep(self.POLL_INTERVAL)
                result = await self.client.get(
                    "https://2captcha.com/res.php",
                    params={
                        "key": self.api_key,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                )
                data = result.json()
                if data.get("status") == 1:
                    return data.get("request")
        except Exception as e:
            logger.error(f"Erro ao interagir com 2captcha: {e}")

        logger.warning("2captcha: timeout ou erro ao resolver CAPTCHA")
        return None

    async def _solve_capsolver(self, site_key: str, page_url: str) -> str | None:
        try:
            r = await self.client.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": self.api_key,
                    "task": {
                        "type": "ReCaptchaV2TaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": site_key,
                    },
                },
            )
            task_id = r.json().get("taskId")
            if not task_id:
                logger.error(f"capsolver rejeitou o task: {r.text}")
                return None

            for _ in range(self.MAX_POLLS):
                await asyncio.sleep(self.POLL_INTERVAL)
                result = await self.client.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                data = result.json()
                if data.get("status") == "ready":
                    return data["solution"]["gRecaptchaResponse"]
        except Exception as e:
            logger.error(f"Erro ao interagir com capsolver: {e}")

        return None

    async def close(self) -> None:
        """Fecha o httpx client."""
        await self.client.aclose()
