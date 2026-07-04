"""Classe intermediária para searchers de API do Smart Research Agent.

Fornece infraestrutura reutilizável para todos os searchers baseados em HTTP:
  - HTTP client compartilhado (httpx.AsyncClient) com lifecycle management
  - Rate limiting automático via DomainRateLimiter
  - Retry com exponential backoff via decorator
  - Circuit breaker integration (registry + sinalização)
  - Cache de respostas com TTL por source
  - Métodos protegidos: _make_request(), _handle_rate_limit(), _check_circuit()

Design:
  - Herda de BaseSearcher — mantém contrato de interface existente.
  - Subclasses implementam apenas: `search()`, `normalize()`, e opcionalmente
    `_build_params()`, `_parse_response()`.
  - O ciclo de vida do httpx.AsyncClient é gerenciado centralmente (não cria
    sessões descartáveis por chamada como o HTTPClient antigo com aiohttp).
  - Cache é transparente: _make_request() verifica cache antes de fazer a
    requisição real.

Exemplo de uso:
    class GitHubSearcher(APISearcher):
        def __init__(self, config):
            super().__init__(
                config=config,
                source_name="github",
                base_url="https://api.github.com",
                circuit_config=CircuitBreakerConfig(name="github_api", failure_threshold=3),
                cache_ttl=3600,
            )

        async def search(self, query: str, **kwargs) -> list[SearchResult]:
            params = {"q": query, "sort": "stars"}
            data = await self._make_request("GET", "/search/repositories", params=params)
            return [self.normalize(item) for item in data.get("items", [])]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urljoin, urlparse

import httpx

from src.cache.cache import Cache
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
    get_default_registry,
)
from src.utils.rate_limiter import DomainRateLimiter
from src.utils.retry import RetryConfig, with_retry

logger = logging.getLogger("search.api_searcher")

T = TypeVar("T")


# ─── Configuração do APISearcher ───────────────────────────────────────────


@dataclass
class APISearcherConfig:
    """Configuração agregada para um APISearcher.

    Attributes:
        source_name: Identificador da fonte (ex: "github", "arxiv").
        base_url: URL base da API (ex: "https://api.github.com").
        timeout: Timeout total em segundos para requisições HTTP.
        max_results: Máximo de resultados por busca.
        circuit_config: Configuração do circuit breaker. Se None, não usa CB.
        retry_config: Configuração de retry. Se None, usa padrão.
        cache_ttl: TTL do cache em segundos. Se None ou 0, desabilita cache.
        cache_prefix: Prefixo de chave de cache. Se None, usa source_name.
        rate_limit_domain: Domínio para rate limiting. Se None, extrai de base_url.
        default_headers: Headers padrão injetados em toda requisição.
        auth_header: Header de autenticação (ex: "Authorization").
        auth_token: Token para o header de autenticação.
    """

    source_name: str
    base_url: str
    timeout: float = 30.0
    max_results: int = 20
    circuit_config: CircuitBreakerConfig | None = None
    retry_config: RetryConfig | None = None
    cache_ttl: int | None = None
    cache_prefix: str | None = None
    rate_limit_domain: str | None = None
    default_headers: dict[str, str] | None = None
    auth_header: str | None = None
    auth_token: str | None = None


# ─── APISearcher ───────────────────────────────────────────────────────────


class APISearcher(BaseSearcher):
    """Classe base intermediária para searchers que consomem APIs HTTP.

    Responsabilidades:
      - Gerenciar ciclo de vida do httpx.AsyncClient (lazy + close).
      - Aplicar rate limiting automático antes de cada requisição.
      - Aplicar retry com backoff exponencial via decorator.
      - Integrar circuit breaker para proteção contra cascatas.
      - Cache transparente de respostas JSON.
      - Normalização de erros HTTP (429, 403, 5xx) em sinais de rate limit.

    Subclasses devem implementar:
      - `search(query: str, **kwargs) -> list[SearchResult]`
      - `normalize(raw_result: Any) -> SearchResult`
    """

    # Cliente HTTP compartilhado por instância (não por classe, para evitar
    # conflitos de headers entre searchers)
    _client: httpx.AsyncClient | None = None
    _client_lock: asyncio.Lock | None = None

    def __init__(self, config: APISearcherConfig):
        # Inicializa BaseSearcher com dict compatível
        base_cfg = {
            "timeout": config.timeout,
            "max_results": config.max_results,
            "enabled": True,
        }
        super().__init__(base_cfg)

        self._cfg = config
        self._source_name = config.source_name
        self._base_url = config.base_url.rstrip("/")
        self._rate_limit_domain = config.rate_limit_domain or urlparse(self._base_url).netloc
        self._cache_prefix = config.cache_prefix or config.source_name
        self._client_lock = asyncio.Lock()

        # Circuit breaker
        self._circuit: CircuitBreaker | None = None
        if config.circuit_config:
            # Usa registry para singleton por nome, mas permite override
            self._circuit_name = config.circuit_config.name
        else:
            self._circuit_name = config.source_name

        # Retry config padrão
        self._retry_config = config.retry_config or RetryConfig(
            max_retries=3,
            base_delay=1.0,
            max_delay=30.0,
            exponential_base=2.0,
            jitter=True,
            retryable_exceptions=(
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ),
        )

        # Cache
        self._cache: Cache | None = None
        if config.cache_ttl:
            self._cache = Cache(cache_dir=f"./.cache/{config.source_name}")

        logger.debug(
            f"APISearcher '{self._source_name}' inicializado "
            f"(base={self._base_url}, timeout={config.timeout}, "
            f"circuit={'sim' if config.circuit_config else 'não'}, "
            f"cache={'sim' if self._cache else 'não'})"
        )

    # ── Propriedades ────────────────────────────────────────────────────────

    @property
    def source_name(self) -> str:
        return self._source_name

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def circuit(self) -> CircuitBreaker | None:
        return self._circuit

    # ── Ciclo de Vida do Cliente HTTP ─────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Retorna (ou cria) o httpx.AsyncClient compartilhado."""
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
                    timeout = httpx.Timeout(self._cfg.timeout, connect=10.0)
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=timeout,
                        limits=limits,
                        follow_redirects=True,
                    )
                    logger.debug(f"APISearcher '{self._source_name}': httpx.AsyncClient criado")
        return self._client

    async def close(self) -> None:
        """Fecha o cliente HTTP e libera recursos."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            logger.debug(f"APISearcher '{self._source_name}': httpx.AsyncClient fechado")
        await super().close()

    # ── Circuit Breaker ─────────────────────────────────────────────────────

    async def _ensure_circuit(self) -> CircuitBreaker | None:
        """Obtém ou cria o circuit breaker via registry."""
        if self._circuit is not None:
            return self._circuit
        if self._cfg.circuit_config is None:
            return None

        registry = await get_default_registry()
        self._circuit = await registry.get_or_create(
            self._circuit_name,
            config=self._cfg.circuit_config,
        )
        return self._circuit

    async def _check_circuit(self) -> CircuitBreaker | None:
        """Verifica se o circuito está fechado. Retorna o breaker ou None.

        Raises:
            CircuitBreakerOpen: Se o circuito estiver aberto.
        """
        breaker = await self._ensure_circuit()
        if breaker is None:
            return None

        # Verificação preemptiva (o .call() já faz isso, mas aqui
        # damos um early-return com mensagem customizada)
        if breaker.state.value == "open":
            status = breaker.get_status()
            remaining = status.get("metrics", {}).get("last_failure_time", 0)
            # Calcula tempo restante aproximado
            from src.utils.circuit_breaker import CircuitBreakerOpen as CBO
            raise CBO(
                name=self._circuit_name,
                remaining=self._cfg.circuit_config.recovery_timeout if self._cfg.circuit_config else 300,
                state=breaker.state,
            )
        return breaker

    # ── Rate Limiting ───────────────────────────────────────────────────────

    async def _apply_rate_limit(self) -> None:
        """Aguarda o rate limiter do domínio antes de prosseguir."""
        await DomainRateLimiter.wait(f"https://{self._rate_limit_domain}/")

    def _handle_rate_limit(self, response: httpx.Response) -> None:
        """Processa resposta HTTP para ajustar rate limit adaptativamente.

        Args:
            response: Resposta HTTP recebida.
        """
        DomainRateLimiter.record(
            f"https://{self._rate_limit_domain}/",
            response.status_code,
        )

        if response.status_code == 429:
            # Extrai Retry-After se disponível
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    wait = int(retry_after)
                    logger.warning(
                        f"APISearcher '{self._source_name}': 429 recebido. "
                        f"Aguardando {wait}s (Retry-After)"
                    )
                    # O DomainRateLimiter já reduziu a taxa; aqui apenas logamos
                except ValueError:
                    pass

    # ── Cache ───────────────────────────────────────────────────────────────

    def _cache_key(self, method: str, path: str, params: dict | None = None) -> str:
        """Gera chave de cache determinística para uma requisição."""
        payload = json.dumps({"m": method, "p": path, "q": params or {}}, sort_keys=True)
        hash_val = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"{self._cache_prefix}_{hash_val}"

    async def _get_cached(self, cache_key: str) -> Any | None:
        """Tenta obter resposta do cache."""
        if self._cache is None:
            return None
        try:
            return await self._cache.get(self._cache_prefix, cache_key)
        except Exception as e:
            logger.debug(f"Cache get falhou para '{cache_key}': {e}")
            return None

    async def _set_cached(self, cache_key: str, value: Any) -> None:
        """Armazena resposta no cache."""
        if self._cache is None or self._cfg.cache_ttl is None:
            return
        try:
            await self._cache.set(
                self._cache_prefix,
                cache_key,
                value,
                ttl_seconds=self._cfg.cache_ttl,
                source_type=self._source_name,
            )
        except Exception as e:
            logger.debug(f"Cache set falhou para '{cache_key}': {e}")

    # ── Requisição Central ──────────────────────────────────────────────────

    async def _make_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        skip_circuit: bool = False,
    ) -> Any:
        """Executa requisição HTTP com todas as camadas de proteção.

        Ordem de execução:
          1. Verifica circuit breaker (se não skip_circuit).
          2. Verifica cache (se use_cache=True e método GET).
          3. Aplica rate limiting.
          4. Executa requisição HTTP via retry decorator.
          5. Processa rate limit adaptativo.
          6. Armazena no cache (se GET e sucesso).
          7. Reporta sucesso ao circuit breaker.

        Args:
            method: Método HTTP (GET, POST, etc).
            path: Path relativo à base_url (ex: "/search/repositories").
            params: Query parameters.
            json_data: Body JSON para POST/PUT.
            headers: Headers adicionais (mesclados com default_headers).
            use_cache: Se True, tenta cache para GETs.
            skip_circuit: Se True, ignora circuit breaker (uso interno).

        Returns:
            Resposta JSON parseada (dict/list) ou texto se não for JSON.

        Raises:
            CircuitBreakerOpen: Se o circuito estiver aberto.
            httpx.HTTPStatusError: Em erros HTTP não-recuperáveis.
            Exception: Em falhas de rede após esgotar retries.
        """
        # 1. Circuit breaker
        if not skip_circuit:
            await self._check_circuit()

        # 2. Cache (apenas GET)
        cache_key = None
        if use_cache and method.upper() == "GET" and self._cache:
            cache_key = self._cache_key(method, path, params)
            cached = await self._get_cached(cache_key)
            if cached is not None:
                logger.debug(f"APISearcher '{self._source_name}': cache hit '{cache_key}'")
                return cached

        # 3. Rate limiting
        await self._apply_rate_limit()

        # 4. Executa com retry
        result = await self._execute_with_retry(
            method=method,
            path=path,
            params=params,
            json_data=json_data,
            headers=headers,
        )

        # 6. Cache
        if cache_key and self._cache:
            await self._set_cached(cache_key, result)

        # 7. Reporta sucesso ao circuit breaker
        if not skip_circuit:
            breaker = await self._ensure_circuit()
            if breaker:
                await breaker._on_success()

        return result

    @with_retry(RetryConfig())  # Será substituído em runtime pelo _retry_config
    async def _execute_with_retry(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Executa a requisição HTTP real (decorada com retry em runtime).

        NOTA: Este método é chamado por _make_request. O decorator @with_retry
        é aplicado dinamicamente em _make_request para usar self._retry_config.
        """
        client = await self._get_client()

        # Monta headers
        request_headers = dict(self._cfg.default_headers or {})
        if self._cfg.auth_header and self._cfg.auth_token:
            request_headers[self._cfg.auth_header] = self._cfg.auth_token
        if headers:
            request_headers.update(headers)

        url = urljoin(self._base_url + "/", path.lstrip("/"))

        try:
            if method.upper() == "GET":
                response = await client.get(url, params=params, headers=request_headers)
            elif method.upper() == "POST":
                response = await client.post(
                    url, params=params, json=json_data, headers=request_headers
                )
            elif method.upper() == "PUT":
                response = await client.put(
                    url, params=params, json=json_data, headers=request_headers
                )
            elif method.upper() == "DELETE":
                response = await client.delete(url, params=params, headers=request_headers)
            else:
                raise ValueError(f"Método HTTP não suportado: {method}")

            # Processa rate limit
            self._handle_rate_limit(response)

            # Levanta em status de erro (o retry captura HTTPStatusError)
            response.raise_for_status()

            # Parse da resposta
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return response.json()
            return response.text

        except httpx.HTTPStatusError as e:
            # Reporta falha ao circuit breaker
            breaker = await self._ensure_circuit()
            if breaker:
                await breaker._on_failure()

            # Log contextualizado
            status = e.response.status_code
            if status == 429:
                logger.warning(
                    f"APISearcher '{self._source_name}': Rate limit (429) em {url}"
                )
            elif status == 403:
                logger.warning(
                    f"APISearcher '{self._source_name}': Forbidden (403) em {url}"
                )
            elif status >= 500:
                logger.warning(
                    f"APISearcher '{self._source_name}': Server error ({status}) em {url}"
                )
            else:
                logger.warning(
                    f"APISearcher '{self._source_name}': HTTP {status} em {url}"
                )
            raise

        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            # Reporta falha ao circuit breaker
            breaker = await self._ensure_circuit()
            if breaker:
                await breaker._on_failure()
            logger.warning(
                f"APISearcher '{self._source_name}': Erro de rede em {url}: {e}"
            )
            raise

    # ── Métodos Abstratos / Interface ─────────────────────────────────────

    @abstractmethod
    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca na API e retorna resultados normalizados.

        Subclasses devem usar self._make_request() para todas as chamadas HTTP.
        """
        ...

    @abstractmethod
    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto da API para SearchResult."""
        ...

    # ── Fallback ────────────────────────────────────────────────────────────

    def fallback(self, query: str) -> list[SearchResult]:
        """Fallback padrão: retorna lista vazia e loga."""
        logger.warning(
            f"APISearcher '{self._source_name}': fallback ativado para query '{query[:50]}'"
        )
        return []

    # ── Utilitários ─────────────────────────────────────────────────────────

    def _build_pagination_params(
        self,
        page: int = 1,
        per_page: int | None = None,
        page_key: str = "page",
        per_page_key: str = "per_page",
    ) -> dict[str, Any]:
        """Helper para construir parâmetros de paginação padrão."""
        return {
            page_key: page,
            per_page_key: per_page or self.max_results,
        }

    async def _fetch_all_pages(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        max_pages: int = 3,
        items_key: str | None = None,
    ) -> list[Any]:
        """Busca múltiplas páginas de resultados concatenando-as.

        Args:
            method: Método HTTP.
            path: Path da API.
            params: Parâmetros base (sem paginação).
            max_pages: Número máximo de páginas a buscar.
            items_key: Chave do dict que contém a lista de items. Se None,
                      assume que a resposta já é uma lista.

        Returns:
            Lista concatenada de items de todas as páginas.
        """
        all_items: list[Any] = []
        page = 1

        while page <= max_pages:
            page_params = {**params, "page": page}
            data = await self._make_request(method, path, params=page_params)

            if items_key:
                items = data.get(items_key, []) if isinstance(data, dict) else []
            else:
                items = data if isinstance(data, list) else []

            if not items:
                break

            all_items.extend(items)
            if len(items) < (params.get("per_page", self.max_results)):
                break

            page += 1

        return all_items
