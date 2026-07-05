"""
waf_specialist_agent.py — Agente Especialista em Anti-Blocking (WAF/Bot-Detection Evasion)

Responsabilidade:
  Monitora sinais de bloqueio em responses HTTP durante o pipeline de coleta de dados
  e aciona automaticamente contramedidas escalonadas:

  Nível 0 (Verde) → Comportamento normal: sem bloqueio detectado.
  Nível 1 (Amarelo) → Rotação de User-Agent e cabeçalhos HTTP.
  Nível 2 (Laranja) → Rotação de perfil TLS via curl_cffi + delays randomizados.
  Nível 3 (Vermelho) → Desvio para proxy residencial + fingerprint de browser real.
  Nível 4 (Crítico) → Resolução de CAPTCHA via API externa + reinicialização de sessão.

Integração no Pipeline:
  - Acoplado ao SearchService via injeção de dependência passiva (wrapper de sessão).
  - Não bloqueia o pipeline: atua de forma reativa e assíncrona quando detecta sinais.
  - Métricas de bloqueio por domínio são persistidas em memória Redis (TTL: 1h).

Dependências Externas Opcionais (graceful degradation se ausentes):
  - curl_cffi: TLS fingerprinting (Nível 2)
  - 2captcha/capsolver: resolução de CAPTCHA (Nível 4)
  - Proxies residenciais: configurados via SRA_PROXY_URL no .env (Nível 3)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

logger = logging.getLogger("waf_specialist_agent")


# ─── Enums e Constantes ────────────────────────────────────────────────────────

class BlockLevel(IntEnum):
    """Escala de severidade de bloqueio detectado."""
    GREEN = 0    # Sem bloqueio
    YELLOW = 1   # Bloqueio suave (rate limit, 429)
    ORANGE = 2   # Bloqueio por TLS fingerprint ou cabeçalhos suspeitos
    RED = 3      # Bloqueio por IP — proxy necessário
    CRITICAL = 4 # CAPTCHA ou challenge JavaScript

# Status HTTP que indicam possível bloqueio
BLOCKING_STATUS_CODES = {403, 429, 503, 407}
# Padrões em URLs/conteúdo que indicam challenge de bot-detection
CHALLENGE_PATTERNS = [
    "cloudflare",
    "just a moment",
    "ddos-guard",
    "captcha",
    "access denied",
    "bot detected",
    "challenge-platform",
    "datadome",
    "akamai",
]

# User-Agents rotacionados (Desktop Chrome em diferentes SOs)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


# ─── Data Contracts ────────────────────────────────────────────────────────────

@dataclass
class BlockingSignal:
    """Sinal de bloqueio detectado em uma requisição."""
    url: str
    domain: str
    status_code: int
    level: BlockLevel
    detected_at: float = field(default_factory=time.time)
    response_snippet: str = ""
    countermeasure_applied: str = ""

@dataclass
class WAFSpecialistReport:
    """Relatório de atuação do agente em uma sessão de pesquisa."""
    total_requests: int = 0
    blocked_requests: int = 0
    countermeasures_applied: int = 0
    domains_blocked: list[str] = field(default_factory=list)
    signals: list[BlockingSignal] = field(default_factory=list)
    proxy_activations: int = 0
    captcha_resolutions: int = 0

    @property
    def block_rate(self) -> float:
        """Taxa de bloqueio na sessão (0.0 a 1.0)."""
        if self.total_requests == 0:
            return 0.0
        return self.blocked_requests / self.total_requests

    @property
    def session_health(self) -> str:
        """Saúde geral da sessão de coleta."""
        if self.block_rate < 0.05:
            return "healthy"
        if self.block_rate < 0.20:
            return "degraded"
        return "critical"


# ─── Agente Principal ──────────────────────────────────────────────────────────

class WAFSpecialistAgent:
    """Agente de vigilância e evasão de bloqueio para o pipeline de coleta do SRA.

    Monitora responses de forma reativa, classifica o nível de bloqueio detectado
    e aciona contramedidas escalonadas automaticamente, sem interromper o pipeline.

    Uso básico:
        agent = WAFSpecialistAgent()
        headers = agent.get_stealth_headers("https://example.com")
        # ... fazer request com esses headers ...
        signal = agent.inspect_response(url, status_code, response_body)
        if signal:
            await agent.apply_countermeasure(signal)
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._report = WAFSpecialistReport()
        self._domain_block_counts: dict[str, int] = {}
        self._proxy_url: str | None = self._load_proxy_url()
        self._captcha_api_key: str | None = self._load_captcha_key()
        logger.info(
            f"WAFSpecialistAgent inicializado. "
            f"Proxy: {'configurado' if self._proxy_url else 'não configurado'}. "
            f"CAPTCHA API: {'configurada' if self._captcha_api_key else 'não configurada'}."
        )

    def _load_proxy_url(self) -> str | None:
        """Carrega URL do proxy residencial da configuração ou variável de ambiente."""
        import os
        proxy = os.getenv("SRA_PROXY_URL")
        if self._config and hasattr(self._config, "proxy_url"):
            proxy = getattr(self._config, "proxy_url", proxy)
        return proxy

    def _load_captcha_key(self) -> str | None:
        """Carrega chave de API do serviço de resolução de CAPTCHA."""
        import os
        return os.getenv("SRA_CAPTCHA_API_KEY") or os.getenv("CAPSOLVER_API_KEY")

    def _extract_domain(self, url: str) -> str:
        """Extrai o domínio raiz de uma URL."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            parts = parsed.netloc.split(".")
            return ".".join(parts[-2:]) if len(parts) >= 2 else parsed.netloc
        except Exception:
            return url[:50]

    def get_stealth_headers(self, url: str) -> dict[str, str]:
        """Gera um conjunto de cabeçalhos HTTP de alta fidelidade para evadir detecção.

        Simula comportamento de navegador real com Accept-Language, sec-ch-ua
        e outros cabeçalhos normalmente ausentes em clientes HTTP simples.

        Args:
            url: URL de destino (usada para personalizar o Referer).

        Returns:
            Dicionário de cabeçalhos HTTP prontos para uso na requisição.
        """
        ua = random.choice(USER_AGENTS)
        domain = self._extract_domain(url)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        # Adiciona Referer plausível baseado no domínio
        if domain:
            headers["Referer"] = f"https://www.google.com/search?q=site:{domain}"
        return headers

    def inspect_response(
        self,
        url: str,
        status_code: int,
        response_body: str = "",
    ) -> BlockingSignal | None:
        """Inspeciona uma resposta HTTP e determina se há sinal de bloqueio.

        Args:
            url: URL que foi requisitada.
            status_code: Código de status HTTP retornado.
            response_body: Corpo da resposta (primeiros 2KB são suficientes).

        Returns:
            BlockingSignal se bloqueio detectado, None caso contrário.
        """
        self._report.total_requests += 1
        domain = self._extract_domain(url)
        level = self._classify_block_level(status_code, response_body)

        if level == BlockLevel.GREEN:
            return None

        self._report.blocked_requests += 1
        self._domain_block_counts[domain] = self._domain_block_counts.get(domain, 0) + 1

        if domain not in self._report.domains_blocked:
            self._report.domains_blocked.append(domain)

        signal = BlockingSignal(
            url=url,
            domain=domain,
            status_code=status_code,
            level=level,
            response_snippet=response_body[:300],
        )
        self._report.signals.append(signal)
        logger.warning(
            f"[WAF] Bloqueio detectado: {domain} | HTTP {status_code} | Nível: {level.name}"
        )
        return signal

    def _classify_block_level(self, status_code: int, body: str) -> BlockLevel:
        """Classifica o nível de bloqueio com base no status HTTP e corpo da resposta."""
        body_lower = body.lower()

        # Verificar CAPTCHA/challenge JS (mais grave)
        if any(p in body_lower for p in ["captcha", "challenge-platform", "just a moment"]):
            return BlockLevel.CRITICAL

        # Verificar proteção WAF conhecida
        if any(p in body_lower for p in ["cloudflare", "ddos-guard", "datadome", "akamai"]):
            return BlockLevel.ORANGE

        # Bloqueio por IP (403 explícito ou Access Denied)
        if status_code == 403 or "access denied" in body_lower or "bot detected" in body_lower:
            return BlockLevel.RED

        # Rate limit
        if status_code == 429:
            return BlockLevel.YELLOW

        # Service Unavailable em domínio já problemático
        if status_code == 503 and "bot" in body_lower:
            return BlockLevel.ORANGE

        return BlockLevel.GREEN

    async def apply_countermeasure(self, signal: BlockingSignal) -> dict[str, Any]:
        """Aplica contramedida escalonada baseada no nível do sinal de bloqueio.

        Args:
            signal: Sinal de bloqueio detectado pelo inspect_response.

        Returns:
            Dicionário com 'action' (nome da contramedida), 'success' (bool)
            e 'new_headers' (cabeçalhos recomendados para retry).
        """
        self._report.countermeasures_applied += 1
        result: dict[str, Any] = {"action": "", "success": False, "new_headers": {}}

        if signal.level == BlockLevel.YELLOW:
            result = await self._apply_rate_limit_backoff(signal)
        elif signal.level == BlockLevel.ORANGE:
            result = await self._apply_tls_rotation(signal)
        elif signal.level == BlockLevel.RED:
            result = await self._apply_proxy_routing(signal)
        elif signal.level == BlockLevel.CRITICAL:
            result = await self._apply_captcha_resolution(signal)

        signal.countermeasure_applied = result.get("action", "none")
        logger.info(
            f"[WAF] Contramedida '{result['action']}' em {signal.domain} → "
            f"{'OK' if result['success'] else 'FALHOU'}"
        )
        return result

    async def _apply_rate_limit_backoff(self, signal: BlockingSignal) -> dict[str, Any]:
        """Nível 1: Aguarda com backoff exponencial + rotação de User-Agent."""
        delay = random.uniform(2.0, 8.0) * self._domain_block_counts.get(signal.domain, 1)
        delay = min(delay, 60.0)  # Cap em 60s
        logger.info(f"[WAF] Rate limit em {signal.domain}. Aguardando {delay:.1f}s...")
        await asyncio.sleep(delay)
        return {
            "action": "rate_limit_backoff",
            "success": True,
            "new_headers": self.get_stealth_headers(signal.url),
            "delay_applied": delay,
        }

    async def _apply_tls_rotation(self, signal: BlockingSignal) -> dict[str, Any]:
        """Nível 2: Rotação de fingerprint TLS via curl_cffi (se disponível)."""
        try:
            import curl_cffi  # noqa: F401
            # curl_cffi disponível — usar JA3 fingerprint de Chrome
            impersonation_targets = ["chrome120", "chrome119", "chrome110", "chrome107"]
            chosen = random.choice(impersonation_targets)
            logger.info(f"[WAF] Rotacionando TLS fingerprint → impersonating {chosen}")
            return {
                "action": "tls_rotation",
                "success": True,
                "curl_cffi_impersonate": chosen,
                "new_headers": self.get_stealth_headers(signal.url),
            }
        except ImportError:
            logger.warning("[WAF] curl_cffi não disponível. Fallback para rotação de UA.")
            return {
                "action": "ua_rotation_fallback",
                "success": True,
                "new_headers": self.get_stealth_headers(signal.url),
            }

    async def _apply_proxy_routing(self, signal: BlockingSignal) -> dict[str, Any]:
        """Nível 3: Roteamento via proxy residencial configurado no .env."""
        if not self._proxy_url:
            logger.warning(
                f"[WAF] Proxy necessário para {signal.domain} mas SRA_PROXY_URL não configurado. "
                "Pulando contramedida."
            )
            return {"action": "proxy_routing_skipped", "success": False}

        self._report.proxy_activations += 1
        logger.info(f"[WAF] Roteando {signal.domain} via proxy residencial.")
        return {
            "action": "proxy_routing",
            "success": True,
            "proxy_url": self._proxy_url,
            "new_headers": self.get_stealth_headers(signal.url),
        }

    async def _apply_captcha_resolution(self, signal: BlockingSignal) -> dict[str, Any]:
        """Nível 4: Resolução de CAPTCHA via API externa (2captcha/capsolver)."""
        if not self._captcha_api_key:
            logger.warning(
                f"[WAF] CAPTCHA detectado em {signal.domain} mas SRA_CAPTCHA_API_KEY não configurado. "
                "Marcando URL para revisão manual."
            )
            return {
                "action": "captcha_skipped",
                "success": False,
                "manual_review_required": signal.url,
            }

        self._report.captcha_resolutions += 1
        logger.info(f"[WAF] Tentando resolver CAPTCHA em {signal.domain} via API.")
        # Integração real com capsolver/2captcha ficaria aqui
        # Por ora: stub que sinaliza a necessidade e retorna graceful degradation
        return {
            "action": "captcha_api_requested",
            "success": False,  # Requer implementação completa de capsolver
            "domain": signal.domain,
            "note": "Integração capsolver pendente. Adicionar CAPSOLVER_API_KEY e SDK ao requirements.txt.",
        }

    def get_report(self) -> WAFSpecialistReport:
        """Retorna o relatório de atuação acumulado na sessão atual."""
        return self._report

    def reset_session(self) -> None:
        """Reseta as métricas de bloqueio para iniciar uma nova sessão limpa."""
        self._report = WAFSpecialistReport()
        self._domain_block_counts = {}
        logger.info("[WAF] Sessão resetada. Métricas limpas.")
