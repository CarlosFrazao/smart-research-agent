"""Classe base abstrata para todos os searchers do Smart Research Agent.

Define a interface padrao que todos os searchers devem implementar:
busca assincrona, normalizacao de resultados e fallback em caso de falha.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import httpx

from src.types import SearchResult
from src.utils.retry import RetryConfig, build_async_retrying

logger = logging.getLogger(__name__)


# ── Exceções de busca ─────────────────────────────────────────────────────────


class SearcherError(RuntimeError):
    """Erro genérico de busca (após esgotar retries)."""

    pass


class CircuitBreakerOpenError(SearcherError):
    """Levantado quando o circuit breaker está OPEN e a chamada é rejeitada."""

    pass


# ── Circuit Breaker ───────────────────────────────────────────────────────────


class CircuitState(str, Enum):
    """Estados possíveis de um circuit breaker de busca."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerConfig:
    """Configuração de limiares e timeouts de um circuit breaker."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        half_open_success_threshold: int = 2,
    ):
        """Inicializa os limiares do circuit breaker.

        Args:
            failure_threshold: número de falhas consecutivas antes de abrir.
            reset_timeout_seconds: tempo de espera no estado OPEN antes do
                teste half-open.
            half_open_success_threshold: sucessos necessários no half-open para
                fechar novamente.
        """
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.half_open_success_threshold = half_open_success_threshold


class CircuitBreaker:
    """Protege chamadas de busca contra falhas em cascata.

    Transiciona entre CLOSED, OPEN e HALF_OPEN conforme o volume de falhas,
    rejeitando chamadas enquanto OPEN para evitar sobrecarregar fontes caídas.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        """Cria um circuit breaker com nome e configuração opcional."""
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        """Estado atual, promovendo OPEN -> HALF_OPEN após o timeout de reset."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.config.reset_timeout_seconds:
                logger.info(
                    "circuit_breaker_half_open name=%s after=%.1fs", self.name, elapsed
                )
                self._state = CircuitState.HALF_OPEN
                self._half_open_success_count = 0
        return self._state

    def before_call(self) -> None:
        """Valida se a chamada pode prosseguir; levanta se o breaker está OPEN."""
        if self.state is CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"circuit breaker '{self.name}' está OPEN — chamadas rejeitadas "
                f"por {self.config.reset_timeout_seconds}s desde a última falha."
            )

    def on_success(self) -> None:
        """Registra uma chamada bem-sucedida e fecha o breaker se aplicável."""
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_success_count += 1
            if self._half_open_success_count >= self.config.half_open_success_threshold:
                logger.info("circuit_breaker_closed name=%s", self.name)
                self._state = CircuitState.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def on_failure(self) -> None:
        """Registra uma falha e abre o breaker ao atingir o limiar."""
        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN:
            logger.warning(
                "circuit_breaker_reopened name=%s (falha durante teste half-open)",
                self.name,
            )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
        elif self._failure_count >= self.config.failure_threshold:
            logger.warning(
                "circuit_breaker_opened name=%s failures=%d threshold=%d",
                self.name,
                self._failure_count,
                self.config.failure_threshold,
            )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def stats(self) -> dict:
        """Retorna um dicionário com nome, estado e contagem de falhas."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
        }


# ── Classe Base ───────────────────────────────────────────────────────────────


class BaseSearcher(ABC):
    """Classe base abstrata para searchers de fontes de dados.

    Define o contrato de interface que todos os searchers devem implementar.
    Fornece atributos comuns (timeout, max_results, enabled), fallback padrao,
    e mecanismos compartilhados de HTTP Client, Retry e Circuit Breaker.
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs: Any):
        """Inicializa o searcher com timeout, limiares de circuit breaker e retry.

        Args:
            config: dicionário de configuração (timeout, max_results, enabled,
                credenciais e parâmetros de circuit breaker/retry). Se ``None``,
                usa ``kwargs``.
            **kwargs: parâmetros alternativos quando ``config`` não é informado.
        """
        if config is None:
            config = kwargs
        elif isinstance(config, str):
            config = {"name": config, **kwargs}

        self.config = config
        self.timeout = config.get("timeout", config.get("http_timeout_seconds", 30))
        self.max_results = config.get("max_results", 20)
        self.enabled = config.get("enabled", True)
        self.name = config.get("name", self.__class__.__name__)

        # Configurações de Circuit Breaker
        cb_failure_threshold = config.get("circuit_breaker_failure_threshold", 5)
        cb_reset_timeout = config.get("circuit_breaker_reset_timeout_seconds", 30.0)
        cb_success_threshold = config.get(
            "circuit_breaker_half_open_success_threshold", 2
        )
        cb_config = CircuitBreakerConfig(
            failure_threshold=cb_failure_threshold,
            reset_timeout_seconds=cb_reset_timeout,
            half_open_success_threshold=cb_success_threshold,
        )
        self.circuit_breaker = CircuitBreaker(name=self.name, config=cb_config)

        # Configurações de Tenacity Retry
        self._retry_config = RetryConfig(
            max_attempts=config.get("retry_max_attempts", 3),
            initial_wait_seconds=config.get("retry_initial_wait_seconds", 0.5),
            max_wait_seconds=config.get("retry_max_wait_seconds", 8.0),
            retry_on=(httpx.TimeoutException, httpx.ConnectError, httpx.ReadError),
        )
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BaseSearcher":
        """Context manager: cria o ``httpx.AsyncClient`` ao entrar."""
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc_info) -> None:
        """Context manager: fecha o ``httpx.AsyncClient`` ao sair."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """Retorna o ``httpx.AsyncClient`` atual, criando um sob demanda."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    @abstractmethod
    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa a busca na fonte de dados e retorna resultados normalizados.

        Args:
            query: Texto da query a buscar.
            **kwargs: Parametros extras especificos do searcher.

        Returns:
            list[SearchResult]: Lista de resultados normalizados.
        """
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_result: Any) -> SearchResult | None:
        """Normaliza um resultado bruto da API para o formato `SearchResult`.

        Args:
            raw_result: Resultado bruto retornado pela API de origem.

        Returns:
            SearchResult | None: Resultado normalizado no formato padrao do SRA,
            ou ``None`` quando o item bruto é inválido/incompleto e deve ser
            descartado (subclasses como NPM/crates.io filtram itens sem os
            campos mínimos). Chamadores devem descartar ``None``.
        """
        raise NotImplementedError

    def fallback(self, query: str) -> list[SearchResult]:
        """Retorna lista vazia e registra aviso de fallback ativado.

        Args:
            query: Query que nao pôde ser executada.

        Returns:
            list[SearchResult]: Lista vazia (fallback padrao).
        """
        logger.warning(f"Fallback ativado para {self.__class__.__name__}: {query[:50]}")
        return []

    async def close(self) -> None:
        """Fecha os recursos e conexões abertas do searcher.

        Encerra o ``httpx.AsyncClient`` (``self._client``) e todos os
        ``aiohttp.HTTPClient`` encontrados nos atributos do searcher
        (incluindo ``self.http`` e quaisquer nomes alternativos). Sem isto,
        cada searcher que usa ``HTTPClient`` vazava uma ``aiohttp.ClientSession``
        não fechada ("Unclosed client session") — cenário observado quando um
        searcher guarda o ``HTTPClient`` em atributo de nome não padrão.
        """
        # Fecha o httpx.AsyncClient padrão.
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
            self._client = None

        # Vara todos os atributos em busca de HTTPClient (aiohttp) para fechar.
        from src.utils.http_client import HTTPClient

        closed: set[int] = set()
        for attr_name in tuple(vars(self).keys()):
            value = getattr(self, attr_name, None)
            if isinstance(value, HTTPClient) and id(value) not in closed:
                closed.add(id(value))
                try:
                    await value.close()
                except Exception:
                    pass

    async def _http_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Faz a chamada HTTP com tratamento de retry Tenacity e tratamento de erros."""
        client = self._ensure_client()
        retrying = build_async_retrying(self._retry_config)

        async for attempt in retrying:
            with attempt:
                response = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
                response.raise_for_status()
                return response

        raise SearcherError(
            f"[{self.name}] retry loop terminou sem resposta nem exceção."
        )

    def stats(self) -> dict:
        return {"searcher": self.name, "circuit_breaker": self.circuit_breaker.stats()}
