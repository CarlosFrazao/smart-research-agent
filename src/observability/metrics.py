from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("sra.metrics")

# Singleton preguiçoso para evitar exceptions na ausência do client
_metrics: dict[str, Any] | None = None


def get_metrics() -> dict[str, Any]:
    global _metrics
    if _metrics is None:
        try:
            from prometheus_client import Counter, Gauge, Histogram

            _metrics = {
                "search_requests": Counter(
                    "sra_search_requests_total",
                    "Total de requisicoes de busca processadas",
                    ["source", "status"],
                ),
                "search_duration": Histogram(
                    "sra_search_duration_seconds",
                    "Tempo gasto para retornar buscas de cada provedor",
                    ["source"],
                    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
                ),
                "llm_tokens": Counter(
                    "sra_llm_tokens_total",
                    "Total de tokens de LLM consumidos",
                    ["provider", "model", "token_type"],
                ),
                "circuit_breaker_state": Gauge(
                    "sra_circuit_breaker_state",
                    "Estado dos circuit breakers (0=closed, 1=half_open, 2=open)",
                    ["source"],
                ),
                "cache_hits": Counter(
                    "sra_cache_hits_total",
                    "Total de acertos na camada de cache",
                    ["source_type"],
                ),
            }
            logger.info("Prometheus metrics registradas com sucesso.")
        except ImportError:
            logger.warning(
                "prometheus-client não está instalado. Métricas desabilitadas."
            )
            _metrics = {}
    return _metrics


def start_metrics_server(port: int = 8001) -> None:
    """Inicia o servidor de escuta HTTP do Prometheus client em thread de background."""
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
        logger.info(f"Prometheus HTTP Metrics Server escutando na porta {port}.")
    except ImportError:
        logger.warning(
            "Falha ao subir Metrics Server (prometheus-client não instalado)."
        )
    except Exception as e:
        logger.warning(f"Não foi possível iniciar o Metrics Server: {e}")


@asynccontextmanager
async def track_search(source: str):
    """Context manager para computar e reportar tempo de busca e status de falhas."""
    metrics = get_metrics()
    start_time = time.monotonic()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        duration = time.monotonic() - start_time
        if "search_requests" in metrics:
            metrics["search_requests"].labels(source=source, status=status).inc()
            metrics["search_duration"].labels(source=source).observe(duration)
