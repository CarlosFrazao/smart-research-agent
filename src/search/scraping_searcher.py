"""ScrapingSearcher — Classe intermediária para searchers de scraping no SRA.

Fornece uma cascata de scrapers (Firecrawl → Spider → Steel → Jina) com:
  - Fallback automático entre scrapers
  - Rate limiting por domínio
  - Circuit breaker por scraper
  - Retry com backoff exponencial
  - Cache de respostas
  - Normalização unificada de resultados

Herda de BaseSearcher e serve como classe base para todos os searchers
que realizam scraping de conteúdo web (ao invés de APIs estruturadas).

Uso:
    class MyScraper(ScrapingSearcher):
        async def search(self, query: str, **kwargs) -> List[SearchResult]:
            url = self._build_url(query)
            raw = await self._cascade_scrape(url)
            return [self.normalize(r) for r in raw]

    scraper = MyScraper(config, scrapers={"firecrawl": fc, "spider": sp})
    results = await scraper.search("python async")
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import (
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
)
from src.utils.retry import retry_call, RetryResult

logger = logging.getLogger(__name__)


# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_SCRAPER_TIMEOUT: float = 30.0
DEFAULT_RATE_LIMIT_RPS: float = 2.0  # requests por segundo por domínio
DEFAULT_MIN_CONTENT_LENGTH: int = 200  # caracteres mínimos para considerar válido
DEFAULT_CASCADE_ORDER: Tuple[str, ...] = ("firecrawl", "spider", "steel", "jina")


# ── Configuração ────────────────────────────────────────────────────────────


@dataclass
class ScrapingConfig:
    """Configuração fina para ScrapingSearcher.

    Attributes:
        timeout: Timeout por requisição de scraping (segundos).
        rate_limit_rps: Máximo de requisições por segundo por domínio.
        min_content_length: Tamanho mínimo do conteúdo para aceitar resultado.
        cascade_order: Ordem de tentativa dos scrapers no fallback.
        cache_enabled: Se True, usa cache para URLs já scrapeadas.
        cache_ttl_seconds: TTL do cache de scraping.
        retry_max_attempts: Tentativas de retry por scraper.
        retry_min_wait: Espera mínima entre retries (segundos).
        retry_max_wait: Espera máxima entre retries (segundos).
        circuit_breaker_failure_threshold: Falhas para abrir circuito.
        circuit_breaker_recovery_timeout: Segundos até tentar HALF_OPEN.
        user_agent: User-Agent customizado (None = usar padrão do scraper).
        respect_robots_txt: Se True, verifica robots.txt antes de scrapear.
        proxy_url: Proxy HTTP/SOCKS opcional.
    """

    timeout: float = DEFAULT_SCRAPER_TIMEOUT
    rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS
    min_content_length: int = DEFAULT_MIN_CONTENT_LENGTH
    cascade_order: Tuple[str, ...] = DEFAULT_CASCADE_ORDER
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    retry_max_attempts: int = 3
    retry_min_wait: float = 1.0
    retry_max_wait: float = 30.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 60.0
    user_agent: Optional[str] = None
    respect_robots_txt: bool = False
    proxy_url: Optional[str] = None


@dataclass
class ScrapeAttempt:
    """Registro de uma tentativa de scraping para observabilidade."""

    scraper_name: str
    url: str
    success: bool
    latency_ms: float
    content_length: int
    error: str = ""
    fallback_triggered: bool = False


# ── ScrapingSearcher ────────────────────────────────────────────────────────


class ScrapingSearcher(BaseSearcher):
    """Classe base intermediária para searchers de scraping com cascade fallback.

    Herda de BaseSearcher e adiciona:
      - Cascata de scrapers com fallback automático
      - Rate limiting por domínio (token bucket)
      - Circuit breaker por scraper
      - Retry com backoff exponencial
      - Cache de URLs scrapeadas
      - Métricas de tentativas

    Args:
        config: Dict de configuração (do Orchestrator/Factory).
        scrapers: Mapa {nome: instância} dos scrapers disponíveis.
        cache: Instância de Cache opcional.
        circuit_breaker_registry: Registro compartilhado de circuit breakers.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        scrapers: Optional[Dict[str, Any]] = None,
        cache: Optional[Any] = None,
        circuit_breaker_registry: Optional[CircuitBreakerRegistry] = None,
    ):
        super().__init__(config)
        self.scraping_config = self._build_scraping_config(config)
        self.scrapers = scrapers or {}
        self.cache = cache

        # Rate limiting: semáforo por domínio (token bucket simplificado)
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._last_request_time: Dict[str, float] = {}
        self._min_interval: float = 1.0 / self.scraping_config.rate_limit_rps

        # Circuit breakers por scraper
        self._cb_registry = circuit_breaker_registry or CircuitBreakerRegistry(
            default_failure_threshold=self.scraping_config.circuit_breaker_failure_threshold,
            default_recovery_timeout=self.scraping_config.circuit_breaker_recovery_timeout,
        )

        # Métricas
        self._attempts: List[ScrapeAttempt] = []

    def _build_scraping_config(self, config: Dict[str, Any]) -> ScrapingConfig:
        """Extrai ScrapingConfig do dict de configuração genérico."""
        return ScrapingConfig(
            timeout=config.get("timeout", DEFAULT_SCRAPER_TIMEOUT),
            rate_limit_rps=config.get("rate_limit_rps", DEFAULT_RATE_LIMIT_RPS),
            min_content_length=config.get(
                "min_content_length", DEFAULT_MIN_CONTENT_LENGTH
            ),
            cascade_order=tuple(
                config.get("cascade_order", list(DEFAULT_CASCADE_ORDER))
            ),
            cache_enabled=config.get("cache_enabled", True),
            cache_ttl_seconds=config.get("cache_ttl_seconds", 3600),
            retry_max_attempts=config.get("retry_max_attempts", 3),
            retry_min_wait=config.get("retry_min_wait", 1.0),
            retry_max_wait=config.get("retry_max_wait", 30.0),
            circuit_breaker_failure_threshold=config.get(
                "circuit_breaker_failure_threshold", 5
            ),
            circuit_breaker_recovery_timeout=config.get(
                "circuit_breaker_recovery_timeout", 60.0
            ),
            user_agent=config.get("user_agent"),
            respect_robots_txt=config.get("respect_robots_txt", False),
            proxy_url=config.get("proxy_url"),
        )

    # ── API Pública ─────────────────────────────────────────────────────────

    async def _scrape_url(
        self, url: str, preferred_scraper: Optional[str] = None
    ) -> Dict[str, Any]:
        """Scrapeia uma URL usando o scraper preferido ou a cascata.

        Args:
            url: URL a ser scrapeada.
            preferred_scraper: Nome do scraper para tentar primeiro (ex: 'firecrawl').

        Returns:
            Dict com pelo menos {'markdown'|'content': str, 'url': str, 'title': str}.

        Raises:
            ScrapingError: Se todos os scrapers da cascata falharem.
        """
        # Cache check
        if self.scraping_config.cache_enabled and self.cache:
            cached = await self._get_cached_scrape(url)
            if cached:
                logger.debug(f"Scraping cache hit: {url[:60]}")
                return cached

        # Rate limiting
        await self._apply_rate_limit(url)

        # Cascata
        result, attempt_log = await self._cascade_scrape(url, preferred_scraper)

        # Cache write
        if result and self.scraping_config.cache_enabled and self.cache:
            await self._set_cached_scrape(url, result)

        self._attempts.extend(attempt_log)
        return result

    async def _cascade_scrape(
        self,
        url: str,
        preferred_scraper: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], List[ScrapeAttempt]]:
        """Executa a cascata de scrapers até obter conteúdo válido.

        Ordem: preferred_scraper (se fornecido) → cascade_order configurado.
        Para cada scraper: circuit breaker → retry com backoff → execução.

        Args:
            url: URL alvo.
            preferred_scraper: Scraper para tentar primeiro.

        Returns:
            Tuple[result_dict, attempt_logs]

        Raises:
            ScrapingError: Se nenhum scraper conseguir conteúdo válido.
        """
        attempts: List[ScrapeAttempt] = []

        # Monta ordem de tentativa
        order: List[str] = []
        if preferred_scraper and preferred_scraper in self.scrapers:
            order.append(preferred_scraper)
        for name in self.scraping_config.cascade_order:
            if name not in order and name in self.scrapers:
                order.append(name)

        if not order:
            raise ScrapingError(f"Nenhum scraper disponível para {url[:60]}")

        for scraper_name in order:
            scraper = self.scrapers.get(scraper_name)
            if not scraper or not getattr(scraper, "enabled", True):
                continue

            start = time.monotonic()
            try:
                result = await self._scrape_with_protection(scraper_name, scraper, url)
                latency_ms = (time.monotonic() - start) * 1000

                if self._is_valid_content(result):
                    attempts.append(
                        ScrapeAttempt(
                            scraper_name=scraper_name,
                            url=url,
                            success=True,
                            latency_ms=latency_ms,
                            content_length=len(
                                result.get("markdown", result.get("content", ""))
                            ),
                        )
                    )
                    logger.info(
                        f"Cascade: sucesso com '{scraper_name}' para {url[:60]} "
                        f"({latency_ms:.0f}ms, {attempts[-1].content_length} chars)"
                    )
                    return result, attempts
                else:
                    attempts.append(
                        ScrapeAttempt(
                            scraper_name=scraper_name,
                            url=url,
                            success=False,
                            latency_ms=latency_ms,
                            content_length=0,
                            error="Conteúdo muito curto ou vazio",
                            fallback_triggered=True,
                        )
                    )
                    logger.warning(
                        f"Cascade: '{scraper_name}' retornou conteúdo inválido para {url[:60]}"
                    )

            except Exception as e:
                latency_ms = (time.monotonic() - start) * 1000
                attempts.append(
                    ScrapeAttempt(
                        scraper_name=scraper_name,
                        url=url,
                        success=False,
                        latency_ms=latency_ms,
                        content_length=0,
                        error=str(e)[:200],
                        fallback_triggered=True,
                    )
                )
                logger.warning(f"Cascade: '{scraper_name}' falhou para {url[:60]}: {e}")
                continue

        # Todos falharam
        error_msg = self._format_cascade_failure(url, attempts)
        raise ScrapingError(error_msg)

    async def _handle_failure(
        self,
        url: str,
        original_error: Exception,
        attempts: List[ScrapeAttempt],
    ) -> Dict[str, Any]:
        """Handler de falha final da cascata — retorna resultado degradado ou levanta.

        Pode ser sobrescrito por subclasses para fallback customizado
        (ex: retornar snippet do search engine, usar cache stale, etc.).

        Args:
            url: URL que falhou.
            original_error: Erro da última tentativa.
            attempts: Log de todas as tentativas.

        Returns:
            Dict com conteúdo mínimo (ou levanta exceção).

        Raises:
            ScrapingError: Por padrão, propaga a falha.
        """
        logger.error(
            f"Scraping cascade falhou completamente para {url[:60]}. "
            f"Tentativas: {len(attempts)}"
        )
        raise ScrapingError(
            f"Todos os scrapers falharam para {url[:60]}: {original_error}"
        ) from original_error

    # ── Métodos protegidos (extensíveis) ────────────────────────────────────

    async def _scrape_with_protection(
        self,
        scraper_name: str,
        scraper: Any,
        url: str,
    ) -> Dict[str, Any]:
        """Executa scraping com circuit breaker + retry + timeout.

        Args:
            scraper_name: Nome identificador do scraper.
            scraper: Instância do scraper (deve ter método `search(url)`).
            url: URL alvo.

        Returns:
            Dict com resultado do scraping.
        """
        cb = self._cb_registry.get(scraper_name)

        async def _do_scrape() -> Dict[str, Any]:
            # Timeout por scraper
            timeout = getattr(scraper, "timeout", self.scraping_config.timeout)
            result = await asyncio.wait_for(
                scraper.search(url),
                timeout=timeout,
            )
            # Normaliza resultado para dict
            return self._normalize_scrape_result(result, url)

        try:
            return await cb.call(_do_scrape)
        except CircuitBreakerOpen:
            raise ScrapingError(f"Circuit breaker OPEN para '{scraper_name}'") from None
        except asyncio.TimeoutError:
            raise ScrapingError(
                f"Timeout em '{scraper_name}' (>{self.scraping_config.timeout}s)"
            )
        except Exception as e:
            # Retry com backoff via utilitário retry_call
            try:
                retry_result: RetryResult = await retry_call(
                    _do_scrape,
                    max_attempts=self.scraping_config.retry_max_attempts,
                    min_wait=self.scraping_config.retry_min_wait,
                    max_wait=self.scraping_config.retry_max_wait,
                    circuit_breaker=cb,
                    expected_exceptions=(Exception,),
                )
                return retry_result.value
            except Exception:
                raise ScrapingError(f"Retry esgotado para '{scraper_name}': {e}") from e

    def _is_valid_content(self, result: Dict[str, Any]) -> bool:
        """Valida se o resultado do scraping tem conteúdo suficiente.

        Pode ser sobrescrito por subclasses para regras de validação customizadas.
        """
        content = result.get(
            "markdown", result.get("content", result.get("description", ""))
        )
        if not content or not isinstance(content, str):
            return False
        return len(content.strip()) >= self.scraping_config.min_content_length

    def _normalize_scrape_result(self, raw: Any, url: str) -> Dict[str, Any]:
        """Normaliza o resultado de diferentes scrapers para um dict padronizado.

        Suporta:
          - Dict direto (Firecrawl, Spider)
          - SearchResult (outros searchers)
          - List[SearchResult] (pega o primeiro)
          - String (conteúdo raw)
        """
        if isinstance(raw, dict):
            return {
                "url": raw.get("url", url),
                "title": raw.get("title", ""),
                "markdown": raw.get(
                    "markdown", raw.get("content", raw.get("text", ""))
                ),
                "metadata": raw.get("metadata", {}),
            }

        if isinstance(raw, list) and len(raw) > 0:
            first = raw[0]
            if isinstance(first, SearchResult):
                return {
                    "url": first.url or url,
                    "title": first.title,
                    "markdown": first.description,
                    "metadata": first.metrics,
                }
            if isinstance(first, dict):
                return self._normalize_scrape_result(first, url)

        if isinstance(raw, SearchResult):
            return {
                "url": raw.url or url,
                "title": raw.title,
                "markdown": raw.description,
                "metadata": raw.metrics,
            }

        if isinstance(raw, str):
            return {"url": url, "title": "", "markdown": raw, "metadata": {}}

        return {"url": url, "title": "", "markdown": str(raw), "metadata": {}}

    # ── Rate Limiting ───────────────────────────────────────────────────────

    async def _apply_rate_limit(self, url: str) -> None:
        """Aplica rate limiting por domínio usando token bucket simplificado.

        Garante que não exceda `rate_limit_rps` requisições por segundo
        para o mesmo domínio.
        """
        domain = urlparse(url).netloc or "unknown"

        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
            self._last_request_time[domain] = 0.0

        async with self._domain_locks[domain]:
            now = time.monotonic()
            elapsed = now - self._last_request_time[domain]
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                logger.debug(f"Rate limit: aguardando {wait:.2f}s para {domain}")
                await asyncio.sleep(wait)
            self._last_request_time[domain] = time.monotonic()

    # ── Cache helpers ───────────────────────────────────────────────────────

    async def _get_cached_scrape(self, url: str) -> Optional[Dict[str, Any]]:
        """Tenta ler resultado do cache para a URL."""
        if not self.cache:
            return None
        try:
            cache_key = f"scrape:{url}"
            cached = await self.cache.get("scraping", cache_key)
            if cached:
                return cached
        except Exception as e:
            logger.debug(f"Erro ao ler cache de scraping: {e}")
        return None

    async def _set_cached_scrape(self, url: str, result: Dict[str, Any]) -> None:
        """Salva resultado no cache com TTL configurado."""
        if not self.cache:
            return
        try:
            cache_key = f"scrape:{url}"
            await self.cache.set(
                "scraping",
                cache_key,
                result,
                ttl_seconds=self.scraping_config.cache_ttl_seconds,
            )
        except Exception as e:
            logger.debug(f"Erro ao escrever cache de scraping: {e}")

    # ── Utilitários ─────────────────────────────────────────────────────────

    def _format_cascade_failure(self, url: str, attempts: List[ScrapeAttempt]) -> str:
        """Formata mensagem de erro detalhada da cascata."""
        lines = [f"Scraping cascade falhou para {url[:60]}:"]
        for a in attempts:
            status = "✓" if a.success else "✗"
            lines.append(
                f"  {status} {a.scraper_name}: {a.error or f'{a.content_length} chars'} "
                f"({a.latency_ms:.0f}ms)"
            )
        return "\n".join(lines)

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna métricas agregadas de scraping para observabilidade."""
        total = len(self._attempts)
        successes = sum(1 for a in self._attempts if a.success)
        failures = total - successes
        avg_latency = sum(a.latency_ms for a in self._attempts) / max(total, 1)

        by_scraper: Dict[str, Dict[str, int]] = {}
        for a in self._attempts:
            if a.scraper_name not in by_scraper:
                by_scraper[a.scraper_name] = {"success": 0, "failure": 0}
            by_scraper[a.scraper_name]["success" if a.success else "failure"] += 1

        return {
            "total_attempts": total,
            "successes": successes,
            "failures": failures,
            "success_rate": successes / max(total, 1),
            "avg_latency_ms": round(avg_latency, 2),
            "by_scraper": by_scraper,
            "circuit_breakers": self._cb_registry.all_metrics(),
        }

    def reset_metrics(self) -> None:
        """Limpa métricas acumuladas."""
        self._attempts.clear()

    # ── Abstract methods (BaseSearcher) ─────────────────────────────────────

    @abstractmethod
    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Método abstrato — deve ser implementado pela subclass.

        Tipicamente:
          1. Constrói URL(s) a partir da query
          2. Chama self._scrape_url(url) ou self._cascade_scrape(url)
          3. Normaliza e retorna List[SearchResult]
        """
        pass

    @abstractmethod
    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza resultado bruto em SearchResult."""
        pass


# ── Exceções ────────────────────────────────────────────────────────────────


class ScrapingError(Exception):
    """Exceção levantada quando toda a cascata de scraping falha."""

    pass


class RateLimitExceeded(Exception):
    """Exceção levantada quando o rate limit por domínio é violado."""

    pass
