"""
Randomiza TLS fingerprint para evitar detecção por Cloudflare/Akamai.
Usa curl_cffi para impersonar browsers reais ao nível do TLS handshake.
"""
import random
import logging
import asyncio
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

BROWSER_IMPERSONATIONS: List[str] = [
    "chrome124", "chrome120", "chrome116", "chrome110",
    "safari17_2", "safari17_0", "safari15_5",
    "firefox120", "edge99",
]


class TLSFingerprintClient:
    """Cliente HTTP que randomiza TLS fingerprint a cada requisição."""

    def __init__(self):
        try:
            from curl_cffi import requests as cffi_requests
            self._session = cffi_requests.Session()
            self._available = True
        except ImportError:
            logger.warning("curl_cffi não instalado. TLS fingerprinting indisponível.")
            self._available = False

    async def get(
        self,
        url: str,
        headers: Optional[Dict] = None,
        timeout: int = 30,
    ) -> Optional[str]:
        """GET com impersonation TLS aleatório em um executor não-bloqueante."""
        if not self._available:
            logger.debug("TLSFingerprintClient indisponível, retornando None")
            return None

        # Executa no threadpool padrão para não bloquear o event loop do asyncio
        loop = asyncio.get_running_loop()
        try:
            impersonate = random.choice(BROWSER_IMPERSONATIONS)
            
            def _sync_get():
                return self._session.get(
                    url,
                    impersonate=impersonate,
                    timeout=timeout,
                    headers=headers or {},
                )

            response = await loop.run_in_executor(None, _sync_get)
            return response.text
        except Exception as e:
            logger.warning(f"TLSFingerprintClient erro no GET para '{url[:50]}': {e}")
            return None

    async def post(
        self,
        url: str,
        json: Optional[dict] = None,
        headers: Optional[Dict] = None,
        timeout: int = 30,
    ) -> Optional[dict]:
        """POST com impersonation TLS aleatório em um executor não-bloqueante."""
        if not self._available:
            logger.debug("TLSFingerprintClient indisponível, retornando None")
            return None

        loop = asyncio.get_running_loop()
        try:
            impersonate = random.choice(BROWSER_IMPERSONATIONS)

            def _sync_post():
                return self._session.post(
                    url,
                    json=json,
                    impersonate=impersonate,
                    timeout=timeout,
                    headers=headers or {},
                )

            response = await loop.run_in_executor(None, _sync_post)
            return response.json()
        except Exception as e:
            logger.warning(f"TLSFingerprintClient erro no POST para '{url[:50]}': {e}")
            return None
