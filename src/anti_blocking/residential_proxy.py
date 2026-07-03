"""
Suporte a proxies residenciais para contornar bloqueios geográficos e de IP.
Providers suportados: BrightData, Smartproxy.
"""

import logging

logger = logging.getLogger(__name__)


class ResidentialProxyProvider:
    PROVIDERS: dict[str, dict] = {
        "brightdata": {
            "host": "brd.superproxy.io",
            "port": 33335,
            "user_format": "{username}-country-{country}",
        },
        "smartproxy": {
            "host": "gate.smartproxy.com",
            "port": 7000,
            "user_format": "{username}",
        },
    }

    def __init__(self, provider: str, username: str, password: str):
        provider = provider.lower()
        if provider not in self.PROVIDERS:
            raise ValueError(
                f"Provider '{provider}' não suportado. Use: {list(self.PROVIDERS.keys())}"
            )
        self.provider_config = self.PROVIDERS[provider]
        self.username = username
        self.password = password
        self.provider = provider

    def get_proxy_url(self, country: str = "us") -> str:
        cfg = self.provider_config
        user = cfg["user_format"].format(
            username=self.username, country=country.lower()
        )
        return f"http://{user}:{self.password}@{cfg['host']}:{cfg['port']}"

    def get_httpx_proxies(self, country: str = "us") -> dict:
        proxy_url = self.get_proxy_url(country)
        return {"http://": proxy_url, "https://": proxy_url}
