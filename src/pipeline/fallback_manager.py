"""Gerenciamento centralizado de fallbacks para o Smart Research Agent.

Fornece `FallbackManager` — um registry e executor de estratégias de fallback
que substitui o sistema ad-hoc espalhado entre `HealthMonitor`, `SearchService`
e `Orchestrator`.

Funcionalidades:
  - Registro de fallbacks por stage (search, scraper, llm, cache, memory, etc.)
  - Estratégias de seleção: round-robin, priority, random, weighted
  - Métricas de uso de fallback (contadores, latência, taxa de sucesso)
  - Integração com CircuitBreaker (não usa fallback se circuito está OPEN)
  - Fallbacks encadeáveis (primary → secondary → tertiary)
  - Fallbacks condicionais (executa apenas se critério atendido)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any, Callable, TypeVar
from collections.abc import Awaitable

from src.utils.circuit_breaker import (
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
    get_default_registry,
)

logger = logging.getLogger("pipeline.fallback_manager")

T = TypeVar("T")


# ─── Enums ───────────────────────────────────────────────────────────────────


class FallbackStrategy(Enum):
    """Estratégia de seleção entre múltiplas alternativas de fallback."""

    PRIORITY = "priority"  # Tenta na ordem registrada (0, 1, 2...)
    ROUND_ROBIN = "round_robin"  # Distribui ciclos entre alternativas
    RANDOM = "random"  # Seleciona aleatoriamente
    WEIGHTED = "weighted"  # Seleciona proporcionalmente aos pesos


class FallbackStatus(StrEnum):
    """Status de execução de uma ação de fallback."""

    SUCCESS = "success"
    FAILURE = "failure"
    CIRCUIT_OPEN = "circuit_open"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class FallbackAction:
    """Uma ação individual de fallback (primária ou alternativa)."""

    name: str
    action: Callable[..., Awaitable[Any]]
    weight: float = 1.0  # Para WEIGHTED strategy
    timeout: float | None = None  # Timeout específico da ação
    circuit_breaker_name: str | None = None  # Nome do CB associado
    condition: Callable[..., bool] | None = None  # Função de condição


@dataclass
class FallbackRegistration:
    """Registro completo de fallbacks para um stage+name."""

    stage: str
    name: str
    primary: FallbackAction
    fallbacks: list[FallbackAction]
    strategy: FallbackStrategy
    max_attempts: int = 3  # Máximo de tentativas (primary + fallbacks)
    global_timeout: float | None = None  # Timeout total da execução
    on_all_failed: Callable[..., Awaitable[Any]] | None = None  # Handler final


@dataclass
class FallbackMetrics:
    """Métricas de uso de fallback para um registro."""

    total_invocations: int = 0
    primary_success: int = 0
    primary_failure: int = 0
    fallback_success: int = 0
    fallback_failure: int = 0
    circuit_open_skips: int = 0
    total_latency_ms: float = 0.0
    fallback_usage_by_name: dict[str, int] = field(default_factory=dict)
    last_failure_reason: str = ""
    last_success_time: float | None = None
    last_failure_time: float | None = None

    @property
    def total_success(self) -> int:
        return self.primary_success + self.fallback_success

    @property
    def total_failure(self) -> int:
        return self.primary_failure + self.fallback_failure

    @property
    def fallback_rate(self) -> float:
        total = self.total_invocations
        if total == 0:
            return 0.0
        return (self.fallback_success + self.fallback_failure) / total

    @property
    def success_rate(self) -> float:
        total = self.total_success + self.total_failure
        if total == 0:
            return 0.0
        return self.total_success / total

    @property
    def average_latency_ms(self) -> float:
        if self.total_invocations == 0:
            return 0.0
        return self.total_latency_ms / self.total_invocations


# ─── FallbackManager ─────────────────────────────────────────────────────────


class FallbackManager:
    """Gerenciador centralizado de fallbacks com múltiplas estratégias.

    Responsabilidades:
      1. Registrar fallbacks por stage/name com estratégia configurável.
      2. Executar ações protegidas por circuit breaker.
      3. Coletar métricas de uso e latência.
      4. Integrar com HealthMonitor via callbacks de status.
    """

    def __init__(self, orchestrator: Any | None = None):
        self._registrations: dict[str, FallbackRegistration] = {}
        self._metrics: dict[str, FallbackMetrics] = {}
        self._round_robin_indices: dict[str, int] = {}
        self._circuit_registry: CircuitBreakerRegistry | None = None
        self._lock = asyncio.Lock()
        self.orchestrator = orchestrator

    def register_all(self) -> None:
        """Mantido para compatibilidade com orquestradores legados."""
        pass

    # ── Inicialização ───────────────────────────────────────────────────────

    async def init(self) -> None:
        """Inicializa referências ao circuit breaker registry."""
        self._circuit_registry = await get_default_registry()
        logger.info("FallbackManager inicializado")

    # ── Registro ────────────────────────────────────────────────────────────

    def register(
        self,
        stage: str,
        name: str,
        primary: Callable[..., Awaitable[Any]],
        fallbacks: list[tuple[str, Callable[..., Awaitable[Any]]]] | None = None,
        strategy: FallbackStrategy = FallbackStrategy.PRIORITY,
        max_attempts: int = 3,
        global_timeout: float | None = None,
        on_all_failed: Callable[..., Awaitable[Any]] | None = None,
        primary_timeout: float | None = None,
        primary_circuit_name: str | None = None,
    ) -> None:
        """Registra um conjunto de fallbacks para um stage+name."""
        key = self._key(stage, name)

        fb_actions = []
        if fallbacks:
            for fb_name, fb_action in fallbacks:
                fb_actions.append(
                    FallbackAction(
                        name=fb_name,
                        action=fb_action,
                        circuit_breaker_name=fb_name,
                    )
                )

        self._registrations[key] = FallbackRegistration(
            stage=stage,
            name=name,
            primary=FallbackAction(
                name=f"{name}_primary",
                action=primary,
                timeout=primary_timeout,
                circuit_breaker_name=primary_circuit_name or name,
            ),
            fallbacks=fb_actions,
            strategy=strategy,
            max_attempts=max_attempts,
            global_timeout=global_timeout,
            on_all_failed=on_all_failed,
        )

        self._metrics[key] = FallbackMetrics()
        self._round_robin_indices[key] = 0

        logger.debug(
            f"FallbackManager: registrado '{key}' "
            f"(strategy={strategy.value}, fallbacks={len(fb_actions)})"
        )

    def unregister(self, stage: str, name: str) -> None:
        """Remove um registro de fallback."""
        key = self._key(stage, name)
        self._registrations.pop(key, None)
        self._metrics.pop(key, None)
        self._round_robin_indices.pop(key, None)
        logger.debug(f"FallbackManager: removido '{key}'")

    # ── Execução ──────────────────────────────────────────────────────────────

    async def execute(
        self,
        stage: str,
        name: str,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Executa ação primária com fallback automático."""
        key = self._key(stage, name)
        reg = self._registrations.get(key)
        if not reg:
            raise KeyError(f"Fallback não registrado: {key}")

        metrics = self._metrics[key]
        args = args or ()
        kwargs = kwargs or {}
        context = context or {}

        start_time = time.monotonic()
        metrics.total_invocations += 1

        # Seleciona ordem de tentativas conforme estratégia
        ordered_actions = self._order_actions(reg, context)

        # Executa tentativas
        last_error: Exception | None = None
        attempts = 0

        for action in ordered_actions:
            if attempts >= reg.max_attempts:
                break
            attempts += 1

            # Verifica circuit breaker
            if await self._is_circuit_open(action):
                metrics.circuit_open_skips += 1
                logger.debug(
                    f"FallbackManager: '{action.name}' pulado (circuito aberto)"
                )
                continue

            # Verifica condição customizada
            if action.condition and not action.condition(*args, **kwargs):
                logger.debug(f"FallbackManager: '{action.name}' pulado (condição)")
                continue

            # Executa com timeout
            action_start = time.monotonic()
            try:
                timeout = action.timeout or reg.global_timeout
                if timeout:
                    result = await asyncio.wait_for(
                        action.action(*args, **kwargs),
                        timeout=timeout,
                    )
                else:
                    result = await action.action(*args, **kwargs)

                # Sucesso
                latency = (time.monotonic() - action_start) * 1000
                metrics.total_latency_ms += latency
                metrics.last_success_time = time.monotonic()
                metrics.fallback_usage_by_name[action.name] = (
                    metrics.fallback_usage_by_name.get(action.name, 0) + 1
                )

                if action.name == reg.primary.name:
                    metrics.primary_success += 1
                    logger.debug(
                        f"FallbackManager: primary '{action.name}' sucesso "
                        f"({latency:.1f}ms)"
                    )
                else:
                    metrics.fallback_success += 1
                    logger.info(
                        f"FallbackManager: fallback '{action.name}' sucesso "
                        f"para '{key}' ({latency:.1f}ms)"
                    )

                return result

            except asyncio.TimeoutError:
                latency = (time.monotonic() - action_start) * 1000
                metrics.total_latency_ms += latency
                metrics.last_failure_reason = "timeout"
                metrics.last_failure_time = time.monotonic()
                last_error = asyncio.TimeoutError(
                    f"Timeout em '{action.name}' após {timeout}s"
                )
                logger.warning(
                    f"FallbackManager: '{action.name}' timeout ({latency:.1f}ms)"
                )

            except CircuitBreakerOpen:
                metrics.circuit_open_skips += 1
                last_error = CircuitBreakerOpen(f"Circuito aberto para '{action.name}'")
                logger.debug(f"FallbackManager: '{action.name}' circuit breaker open")

            except Exception as e:
                latency = (time.monotonic() - action_start) * 1000
                metrics.total_latency_ms += latency
                metrics.last_failure_reason = str(e)
                metrics.last_failure_time = time.monotonic()
                last_error = e

                if action.name == reg.primary.name:
                    metrics.primary_failure += 1
                else:
                    metrics.fallback_failure += 1

                logger.warning(
                    f"FallbackManager: '{action.name}' falhou: {e} ({latency:.1f}ms)"
                )

        # Todas as alternativas falharam
        total_latency = (time.monotonic() - start_time) * 1000
        logger.error(
            f"FallbackManager: todas as alternativas falharam para '{key}' "
            f"({total_latency:.1f}ms, {attempts} tentativas)"
        )

        # Handler customizado
        if reg.on_all_failed:
            try:
                return await reg.on_all_failed(*args, **kwargs)
            except Exception as e:
                logger.error(f"FallbackManager: on_all_failed também falhou: {e}")
                last_error = e

        raise FallbackExhaustedError(
            stage=stage,
            name=name,
            attempts=attempts,
            last_error=last_error,
            metrics=metrics,
        )

    # ── Estratégias de Seleção ──────────────────────────────────────────────

    def _order_actions(
        self,
        reg: FallbackRegistration,
        context: dict[str, Any],
    ) -> list[FallbackAction]:
        """Ordena ações conforme a estratégia configurada."""
        actions = [reg.primary] + reg.fallbacks

        if reg.strategy == FallbackStrategy.PRIORITY:
            return actions

        elif reg.strategy == FallbackStrategy.ROUND_ROBIN:
            key = self._key(reg.stage, reg.name)
            idx = self._round_robin_indices.get(key, 0)
            if len(reg.fallbacks) > 1:
                rotated = reg.fallbacks[idx:] + reg.fallbacks[:idx]
                self._round_robin_indices[key] = (idx + 1) % len(reg.fallbacks)
                return [reg.primary] + rotated
            return actions

        elif reg.strategy == FallbackStrategy.RANDOM:
            shuffled = reg.fallbacks.copy()
            random.shuffle(shuffled)
            return [reg.primary] + shuffled

        elif reg.strategy == FallbackStrategy.WEIGHTED:
            if reg.fallbacks:
                weights = [fb.weight for fb in reg.fallbacks]
                total = sum(weights)
                if total > 0:
                    r = random.uniform(0, total)
                    cumulative = 0.0
                    for fb in reg.fallbacks:
                        cumulative += fb.weight
                        if r <= cumulative:
                            return [reg.primary, fb]
            return actions

        return actions

    # ── Circuit Breaker Integration ─────────────────────────────────────────

    async def _is_circuit_open(self, action: FallbackAction) -> bool:
        """Verifica se o circuit breaker da ação está aberto."""
        if not action.circuit_breaker_name or not self._circuit_registry:
            return False

        try:
            breaker = await self._circuit_registry.get_async(
                action.circuit_breaker_name
            )
            if breaker is None:
                return False
            return breaker.state.value == "open"
        except Exception as e:
            logger.debug(
                f"FallbackManager: erro ao ler circuit breaker '{action.circuit_breaker_name}': {e}"
            )
            return False

    # ── Métricas ──────────────────────────────────────────────────────────────

    def get_metrics(
        self, stage: str | None = None, name: str | None = None
    ) -> dict[str, FallbackMetrics]:
        """Retorna métricas de fallback."""
        if stage and name:
            key = self._key(stage, name)
            return {key: self._metrics.get(key, FallbackMetrics())}

        if stage:
            return {k: v for k, v in self._metrics.items() if k.startswith(f"{stage}:")}

        return dict(self._metrics)

    def get_all_metrics_summary(self) -> dict[str, Any]:
        """Retorna resumo agregado de todas as métricas."""
        total_invocations = sum(m.total_invocations for m in self._metrics.values())
        total_success = sum(m.total_success for m in self._metrics.values())
        total_fallbacks = sum(
            m.fallback_success + m.fallback_failure for m in self._metrics.values()
        )
        total_circuit_skips = sum(m.circuit_open_skips for m in self._metrics.values())

        return {
            "total_registrations": len(self._registrations),
            "total_invocations": total_invocations,
            "total_success": total_success,
            "total_fallbacks_triggered": total_fallbacks,
            "total_circuit_skips": total_circuit_skips,
            "overall_success_rate": total_success / max(total_invocations, 1),
            "overall_fallback_rate": total_fallbacks / max(total_invocations, 1),
            "by_stage": self._aggregate_by_stage(),
        }

    def _aggregate_by_stage(self) -> dict[str, dict[str, Any]]:
        """Agrega métricas por stage."""
        stages: dict[str, list[FallbackMetrics]] = {}
        for key, metrics in self._metrics.items():
            stage = key.split(":")[0]
            stages.setdefault(stage, []).append(metrics)

        return {
            stage: {
                "invocations": sum(m.total_invocations for m in metrics_list),
                "success_rate": (
                    sum(m.total_success for m in metrics_list)
                    / max(
                        sum(m.total_success + m.total_failure for m in metrics_list), 1
                    )
                ),
                "fallback_rate": (
                    sum(m.fallback_success + m.fallback_failure for m in metrics_list)
                    / max(sum(m.total_invocations for m in metrics_list), 1)
                ),
            }
            for stage, metrics_list in stages.items()
        }

    def reset_metrics(self, stage: str | None = None, name: str | None = None) -> None:
        """Reseta métricas."""
        if stage and name:
            key = self._key(stage, name)
            self._metrics[key] = FallbackMetrics()
        elif stage:
            for key in list(self._metrics.keys()):
                if key.startswith(f"{stage}:"):
                    self._metrics[key] = FallbackMetrics()
        else:
            for key in self._metrics:
                self._metrics[key] = FallbackMetrics()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _key(stage: str, name: str) -> str:
        return f"{stage}:{name}"

    def list_registrations(self) -> list[dict[str, Any]]:
        """Lista todos os registros ativos."""
        return [
            {
                "stage": reg.stage,
                "name": reg.name,
                "strategy": reg.strategy.value,
                "fallbacks_count": len(reg.fallbacks),
                "max_attempts": reg.max_attempts,
            }
            for reg in self._registrations.values()
        ]


# ─── Exceções ────────────────────────────────────────────────────────────────


class FallbackExhaustedError(Exception):
    """Todas as alternativas de fallback foram esgotadas."""

    def __init__(
        self,
        stage: str,
        name: str,
        attempts: int,
        last_error: Exception | None,
        metrics: FallbackMetrics,
    ):
        self.stage = stage
        self.name = name
        self.attempts = attempts
        self.last_error = last_error
        self.metrics = metrics

        msg = (
            f"Fallback esgotado para '{stage}:{name}' "
            f"({attempts} tentativas). "
            f"Último erro: {last_error}"
        )
        super().__init__(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "attempts": self.attempts,
            "last_error": str(self.last_error) if self.last_error else None,
            "metrics": {
                "total_invocations": self.metrics.total_invocations,
                "success_rate": self.metrics.success_rate,
                "fallback_rate": self.metrics.fallback_rate,
            },
        }


# ─── Decorator de conveniência ───────────────────────────────────────────────


def with_fallback(
    stage: str,
    name: str,
    fallback_manager: FallbackManager | None = None,
):
    """Decorator que envolve uma função com fallback automático.

    NOTA: Requer que o fallback esteja previamente registrado no manager.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            fm = fallback_manager or _get_default_manager()
            return await fm.execute(
                stage=stage,
                name=name,
                args=args,
                kwargs=kwargs,
            )

        return wrapper

    return decorator


# ─── Singleton global ────────────────────────────────────────────────────────

_default_manager: FallbackManager | None = None
_manager_lock = asyncio.Lock()


async def get_default_manager() -> FallbackManager:
    """Retorna o FallbackManager global padrão (lazy singleton)."""
    global _default_manager
    if _default_manager is None:
        async with _manager_lock:
            if _default_manager is None:
                _default_manager = FallbackManager()
                await _default_manager.init()
    return _default_manager


def set_default_manager(manager: FallbackManager) -> None:
    """Substitui o manager global (útil para testes)."""
    global _default_manager
    _default_manager = manager


def _get_default_manager() -> FallbackManager:
    """Versão síncrona para uso no decorator (assume já inicializado)."""
    if _default_manager is None:
        raise RuntimeError(
            "FallbackManager não inicializado. Chame get_default_manager() primeiro."
        )
    return _default_manager
