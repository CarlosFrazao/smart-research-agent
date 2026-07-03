"""Gera perfis completos de browser fingerprint para anti-detecção."""

import random

BROWSER_PROFILES: list[dict] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
        "timezone": "America/New_York",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "platform": "Win32",
        "color_depth": 24,
        "languages": ["en-US", "en"],
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
        "timezone": "America/Los_Angeles",
        "webgl_vendor": "Google Inc. (Apple)",
        "platform": "MacIntel",
        "color_depth": 30,
        "languages": ["en-US", "en"],
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0",
        "viewport": {"width": 1366, "height": 768},
        "locale": "pt-BR",
        "timezone": "America/Sao_Paulo",
        "webgl_vendor": "Mozilla",
        "platform": "Win32",
        "color_depth": 24,
        "languages": ["pt-BR", "pt", "en"],
    },
]


class BrowserFingerprintGenerator:
    @staticmethod
    def generate() -> dict:
        """Retorna um perfil aleatório completo de browser."""
        return random.choice(BROWSER_PROFILES)

    @staticmethod
    def random_user_agent() -> str:
        """Retorna o User-Agent de um perfil aleatório."""
        return random.choice(BROWSER_PROFILES)["user_agent"]

    @staticmethod
    def random_viewport() -> dict:
        """Retorna o viewport de um perfil aleatório."""
        return random.choice(BROWSER_PROFILES)["viewport"]
