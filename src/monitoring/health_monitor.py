"""
health_monitor.py — Monitor de Saúde de Serviços e Fallback Automático

Verifica periodicamente todos os serviços críticos do SRA:
  - Containers Docker (Firecrawl, Redis, PostgreSQL, SearXNG, ChromaDB)
  - Provedores de LLM (OpenAI, Anthropic, OpenRouter, Ollama local)
  - ProxyManager (pool de proxies ativos)

Fallback automático:
  - LLM principal indisponível → Ollama local
  - ChromaDB offline → cliente efêmero em memória
  - SearXNG offline → DuckDuckGo como fallback

Skill: root-cause-analysis (Tratamento de exceções e resiliência operacional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import StrEnum
from typing import Any
from collections.abc import Callable

import httpx

from src.utils.circuit_breaker import CircuitBreakerRegistry, CircuitState

logger = logging.getLogger(__name__)

# Limiar de falhas consecutivas para abrir o circuito de uma fonte de busca.
# Corresponde ao comportamento anterior (desabilitar após 3 falhas), agora
# imposto por um circuit breaker real em vez de um contador solto.
_SOURCE_FAILURE_THRESHOLD = 3
_SOURCE_RECOVERY_TIMEOUT_SECONDS = 300.0  # 5 min até liberar tentativa de teste


# ─── Enumerações e Contratos ─────────────────────────────────────────────────


class ServiceStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class ServiceCheck:
    """Configuração de verificação de um serviço."""

    name: str
    url: str
    timeout_seconds: float = 3.0
    expected_status_codes: list[int] = field(default_factory=lambda: [200])
    fallback_action: str | None = None  # Nome da ação de fallback registrada
    critical: bool = False  # Se True, dispara alerta imediato


@dataclass
class ServiceHealthResult:
    """Resultado de uma verificação de saúde."""

    name: str
    status: ServiceStatus
    latency_ms: float
    checked_at: str
    detail: str = ""
    fallback_triggered: bool = False


@dataclass
class HealthSnapshot:
    """Snapshot completo do estado de saúde de todos os serviços."""

    timestamp: str
    services: dict[str, ServiceHealthResult]
    overall_status: ServiceStatus
    alerts: list[str]
    circuit_breakers: dict[str, dict] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.overall_status == ServiceStatus.HEALTHY

    def to_markdown(self) -> str:
        """Formata o snapshot como tabela Markdown para logs/dashboard."""
        icon_map = {
            ServiceStatus.HEALTHY: "🟢",
            ServiceStatus.DEGRADED: "🟡",
            ServiceStatus.OFFLINE: "🔴",
            ServiceStatus.UNKNOWN: "⚪",
        }
        rows = ["| Serviço | Status | Latência | Detalhe |", "|---|---|---|---|"]
        for name, r in self.services.items():
            icon = icon_map.get(r.status, "⚪")
            fallback = " *(fallback ativo)*" if r.fallback_triggered else ""
            rows.append(
                f"| {name} | {icon} {r.status.value}{fallback} | {r.latency_ms:.0f}ms | {r.detail[:60]} |"
            )

        alerts_section = ""
        if self.alerts:
            alerts_section = "\n\n**⚠️ Alertas:**\n" + "\n".join(
                f"- {a}" for a in self.alerts
            )

        cb_section = ""
        if self.circuit_breakers:
            cb_icon = {"closed": "🟢", "open": "🔴", "half_open": "🟡"}
            cb_rows = [
                "\n\n**Circuit Breakers (fontes de busca):**",
                "| Fonte | Estado | Falhas | Último erro |",
                "|---|---|---|---|",
            ]
            for name, m in self.circuit_breakers.items():
                icon = cb_icon.get(m["state"], "⚪")
                last_error = (m.get("last_error") or "-")[:50]
                cb_rows.append(
                    f"| {name} | {icon} {m['state']} | "
                    f"{m['failure_count']}/{m['failure_threshold']} | {last_error} |"
                )
            cb_section = "\n".join(cb_rows)

        overall_icon = icon_map.get(self.overall_status, "⚪")
        return (
            f"## Health Monitor — {self.timestamp}\n\n"
            f"**Status Geral:** {overall_icon} {self.overall_status.value}\n\n"
            + "\n".join(rows)
            + alerts_section
            + cb_section
        )


# ─── HealthMonitor ───────────────────────────────────────────────────────────


class HealthMonitor:
    """
    Monitor assíncrono de saúde com fallback automático.

    Uso:
        monitor = HealthMonitor()
        snapshot = await monitor.check_all()
        print(snapshot.to_markdown())

        # Verificação periódica em background:
        await monitor.start_background_loop(interval_seconds=120)
    """

    # Serviços padrão checados (override via environment variables)
    _DEFAULT_SERVICES: list[ServiceCheck] = [
        ServiceCheck(
            name="firecrawl",
            url=os.environ.get("FIRECRAWL_HEALTH_URL", "http://localhost:3022/"),
            timeout_seconds=5.0,
            expected_status_codes=[200],
            fallback_action="disable_firecrawl",
            critical=True,
        ),
        ServiceCheck(
            name="redis",
            url=os.environ.get("REDIS_HEALTH_URL", "redis://localhost:6379"),
            timeout_seconds=2.0,
            expected_status_codes=[200],
            fallback_action="disable_cache",
            critical=True,
        ),
        ServiceCheck(
            name="searxng",
            url=os.environ.get("SEARXNG_HEALTH_URL", "http://127.0.0.1:3023/healthz"),
            timeout_seconds=3.0,
            expected_status_codes=[200],
            fallback_action="fallback_to_duckduckgo",
            critical=False,
        ),
        ServiceCheck(
            name="chromadb",
            url=os.environ.get(
                "CHROMADB_HEALTH_URL",
                "http://127.0.0.1:3024/api/v2/tenants/default_tenant",
            ),
            timeout_seconds=3.0,
            expected_status_codes=[200],
            fallback_action="use_ephemeral_chroma",
            critical=False,
        ),
        ServiceCheck(
            name="ollama",
            url=os.environ.get("OLLAMA_HEALTH_URL", "http://localhost:11434/api/tags"),
            timeout_seconds=3.0,
            expected_status_codes=[200],
            fallback_action=None,
            critical=False,
        ),
    ]

    def __init__(
        self,
        extra_services: list[ServiceCheck] | None = None,
        on_status_change: Callable[[str, ServiceStatus, ServiceStatus], None]
        | None = None,
    ) -> None:
        self.services: list[ServiceCheck] = list(self._DEFAULT_SERVICES)
        if extra_services:
            self.services.extend(extra_services)

        # Callback chamado quando o status de um serviço muda
        self._on_status_change = on_status_change

        # Estado interno: último status por serviço (para detectar mudanças)
        self._last_status: dict[str, ServiceStatus] = {}

        # Ações de fallback registradas
        self._fallback_actions: dict[str, Callable] = {}

        # Histórico de snapshots (últimos 10)
        self._history: list[HealthSnapshot] = []

        # Referência ao orchestrator (injetada externamente se necessário)
        self.orchestrator: Any | None = None

    # ── Verificação Principal ─────────────────────────────────────────────────

    async def check_all(self) -> HealthSnapshot:
        """Executa verificações paralelas de todos os serviços."""
        tasks = [self._check_service(svc) for svc in self.services]
        results_list: list[ServiceHealthResult] = await asyncio.gather(*tasks)

        results = {r.name: r for r in results_list}
        alerts = self._compute_alerts(results)
        overall = self._compute_overall(results)

        snapshot = HealthSnapshot(
            timestamp=datetime.now(UTC).isoformat(),
            services=results,
            overall_status=overall,
            alerts=alerts,
            circuit_breakers=self._active_source_breaker_metrics(),
        )

        # Dispara fallbacks para serviços offline
        for svc_check in self.services:
            result = results.get(svc_check.name)
            if (
                result
                and result.status == ServiceStatus.OFFLINE
                and svc_check.fallback_action
            ):
                await self._trigger_fallback(svc_check, result)
                result.fallback_triggered = True

        # Detecta mudanças de status e chama callback
        for name, result in results.items():
            prev = self._last_status.get(name, ServiceStatus.UNKNOWN)
            if result.status != prev:
                logger.info(
                    f"HealthMonitor: '{name}' mudou de {prev.value} → {result.status.value}"
                )
                if self._on_status_change:
                    self._on_status_change(name, prev, result.status)
                self._last_status[name] = result.status

        self._history.append(snapshot)
        if len(self._history) > 10:
            self._history.pop(0)

        return snapshot

    async def _check_service(self, svc: ServiceCheck) -> ServiceHealthResult:
        """Verifica um único serviço via HTTP GET ou conexão TCP (para Redis)."""
        start = time.monotonic()
        checked_at = datetime.now(UTC).isoformat()

        # Check de conexão TCP puro para o Redis (não responde HTTP GET)
        if svc.name == "redis" or "6379" in svc.url or svc.url.startswith("redis://"):
            try:
                # Limpa URL para extrair host e porta
                url_clean = svc.url.replace("redis://", "").replace("http://", "")
                if "/" in url_clean:
                    url_clean = url_clean.split("/")[0]
                host = "localhost"
                port = 6379
                if ":" in url_clean:
                    parts = url_clean.split(":")
                    host = parts[0]
                    port = int(parts[1])

                # Abre conexão TCP
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=svc.timeout_seconds
                )
                writer.close()
                await writer.wait_closed()
                latency_ms = (time.monotonic() - start) * 1000
                return ServiceHealthResult(
                    name=svc.name,
                    status=ServiceStatus.HEALTHY,
                    latency_ms=latency_ms,
                    checked_at=checked_at,
                    detail="TCP Connection Success",
                )
            except Exception as e:
                latency_ms = (time.monotonic() - start) * 1000
                return ServiceHealthResult(
                    name=svc.name,
                    status=ServiceStatus.OFFLINE,
                    latency_ms=latency_ms,
                    checked_at=checked_at,
                    detail=f"TCP Fail: {str(e)[:80]}",
                )

        try:
            async with httpx.AsyncClient(timeout=svc.timeout_seconds) as client:
                response = await client.get(svc.url)
                latency_ms = (time.monotonic() - start) * 1000

                if response.status_code in svc.expected_status_codes:
                    return ServiceHealthResult(
                        name=svc.name,
                        status=ServiceStatus.HEALTHY,
                        latency_ms=latency_ms,
                        checked_at=checked_at,
                        detail=f"HTTP {response.status_code}",
                    )
                else:
                    return ServiceHealthResult(
                        name=svc.name,
                        status=ServiceStatus.DEGRADED,
                        latency_ms=latency_ms,
                        checked_at=checked_at,
                        detail=f"HTTP {response.status_code} (inesperado)",
                    )

        except httpx.ConnectError:
            latency_ms = (time.monotonic() - start) * 1000
            return ServiceHealthResult(
                name=svc.name,
                status=ServiceStatus.OFFLINE,
                latency_ms=latency_ms,
                checked_at=checked_at,
                detail="Connection refused",
            )
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ServiceHealthResult(
                name=svc.name,
                status=ServiceStatus.OFFLINE,
                latency_ms=latency_ms,
                checked_at=checked_at,
                detail=str(e)[:80],
            )

    # ── Fallbacks ─────────────────────────────────────────────────────────────

    def register_fallback(self, action_name: str, fn: Callable) -> None:
        """Registra uma ação de fallback nomeada."""
        self._fallback_actions[action_name] = fn

    async def _trigger_fallback(
        self, svc: ServiceCheck, result: ServiceHealthResult
    ) -> None:
        """Executa a ação de fallback associada ao serviço offline."""
        action_name = svc.fallback_action
        if not action_name:
            return

        fn = self._fallback_actions.get(action_name)
        if fn:
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn(svc, result)
                else:
                    fn(svc, result)
                logger.info(
                    f"HealthMonitor: fallback '{action_name}' executado para '{svc.name}'."
                )
            except Exception as e:
                logger.error(f"HealthMonitor: fallback '{action_name}' falhou: {e}")
        else:
            logger.warning(
                f"HealthMonitor: ação de fallback '{action_name}' não registrada para '{svc.name}'."
            )

    # ── Loop Background ───────────────────────────────────────────────────────

    async def start_background_loop(self, interval_seconds: int = 120) -> None:
        """
        Inicia o loop de verificação periódica em background.
        Deve ser aguardado ou criado como task: asyncio.create_task(monitor.start_background_loop())
        """
        logger.info(f"HealthMonitor: loop iniciado (intervalo={interval_seconds}s)")
        while True:
            try:
                snapshot = await self.check_all()
                if snapshot.alerts:
                    logger.warning(
                        f"HealthMonitor: {len(snapshot.alerts)} alerta(s) — "
                        + ", ".join(snapshot.alerts[:3])
                    )
            except Exception as e:
                logger.error(f"HealthMonitor: erro no loop de verificação: {e}")
            await asyncio.sleep(interval_seconds)

    # ── Utilitários ───────────────────────────────────────────────────────────

    def _compute_overall(
        self, results: dict[str, ServiceHealthResult]
    ) -> ServiceStatus:
        """Calcula o status geral com base nos serviços críticos."""
        critical_names = {svc.name for svc in self.services if svc.critical}

        for name, result in results.items():
            if name in critical_names and result.status == ServiceStatus.OFFLINE:
                return ServiceStatus.DEGRADED

        has_degraded = any(r.status == ServiceStatus.DEGRADED for r in results.values())
        has_offline = any(r.status == ServiceStatus.OFFLINE for r in results.values())

        if has_offline and not has_degraded:
            return ServiceStatus.DEGRADED
        if has_degraded:
            return ServiceStatus.DEGRADED
        return ServiceStatus.HEALTHY

    def _compute_alerts(self, results: dict[str, ServiceHealthResult]) -> list[str]:
        alerts = []
        for svc in self.services:
            r = results.get(svc.name)
            if r and r.status == ServiceStatus.OFFLINE:
                severity = "CRÍTICO" if svc.critical else "AVISO"
                alerts.append(f"[{severity}] '{svc.name}' offline — {r.detail}")
        return alerts

    def get_history(self) -> list[HealthSnapshot]:
        """Retorna os últimos 10 snapshots de saúde."""
        return list(self._history)

    def get_last_snapshot(self) -> HealthSnapshot | None:
        """Retorna o snapshot mais recente ou None."""
        return self._history[-1] if self._history else None

    # ── Integração com Circuit Breaker (MEL-6.2) ────────────────────────────────
    #
    # Cada fonte de busca tem seu próprio CircuitBreaker (CLOSED/OPEN/HALF_OPEN)
    # no registry compartilhado de `src.utils.circuit_breaker`. O HealthMonitor
    # não mantém mais contadores de falha próprios — ele apenas reporta
    # resultados (sucesso/falha) e consulta o estado do disjuntor.

    def _breaker(self, source: str):
        return CircuitBreakerRegistry.get(
            source,
            failure_threshold=_SOURCE_FAILURE_THRESHOLD,
            recovery_timeout=_SOURCE_RECOVERY_TIMEOUT_SECONDS,
        )

    def report_failure(self, source: str, error: str) -> None:
        """Reporta uma falha de busca ao circuit breaker da fonte.

        Ao atingir o limiar de falhas consecutivas, o circuito abre (OPEN) e a
        fonte é desabilitada até `recovery_timeout` expirar, quando uma
        tentativa de teste (HALF_OPEN) é liberada automaticamente.
        """
        breaker = self._breaker(source)
        was_open = breaker.state == CircuitState.OPEN

        breaker.record_failure(error)

        logger.warning(
            f"HealthMonitor: falha relatada na fonte '{source}'. "
            f"Contador: {breaker.failure_count}/{breaker.failure_threshold}. Erro: {error}"
        )

        if breaker.state == CircuitState.OPEN and not was_open:
            logger.error(
                f"🚨 HealthMonitor: circuito ABERTO para fonte '{source.upper()}' "
                f"após {breaker.failure_count} falhas consecutivas — desabilitando temporariamente."
            )
            self._set_searcher_enabled(source, False)

    def report_success(self, source: str) -> None:
        """Reporta uma busca bem-sucedida ao circuit breaker da fonte.

        Fecha o circuito (CLOSED) se a fonte estava em HALF_OPEN testando
        recuperação, reabilitando-a automaticamente.
        """
        breaker = self._breaker(source)
        was_half_open = breaker.state == CircuitState.HALF_OPEN

        breaker.record_success()

        if was_half_open and breaker.state == CircuitState.CLOSED:
            logger.info(
                f"✅ HealthMonitor: fonte '{source}' recuperada — circuito fechado, reabilitando."
            )
            self._set_searcher_enabled(source, True)

    def _set_searcher_enabled(self, source: str, enabled: bool) -> None:
        if self.orchestrator and hasattr(self.orchestrator, "searchers"):
            searcher = self.orchestrator.searchers.get(source)
            if searcher:
                searcher.enabled = enabled

    def get_active_sources(self) -> list[str]:
        """Retorna os nomes de fontes de busca que podem ser tentadas agora.

        Uma fonte está ativa se seu circuit breaker está CLOSED, ou HALF_OPEN
        liberando uma tentativa de teste (o breaker gerencia a transição
        automática OPEN → HALF_OPEN quando `recovery_timeout` expira).
        """
        if self.orchestrator and hasattr(self.orchestrator, "searchers"):
            names = list(self.orchestrator.searchers.keys())
        else:
            names = [
                "hackernews",
                "github",
                "reddit",
                "arxiv",
                "producthunt",
                "awesome",
                "web",
                "firecrawl",
            ]

        active = []
        for name in names:
            breaker = self._breaker(name)
            allowed = breaker.allow_request()
            self._set_searcher_enabled(name, allowed)
            if allowed:
                active.append(name)
        return active

    def _active_source_breaker_metrics(self) -> dict[str, dict]:
        """Métricas de todos os circuit breakers de fonte já criados até agora."""
        return CircuitBreakerRegistry.metrics_all()

    def get_circuit_breaker_status(self, source: str) -> dict:
        """Retorna as métricas detalhadas do circuit breaker de uma fonte específica."""
        return self._breaker(source).metrics
