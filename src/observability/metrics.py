from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("sra.metrics")

# Singleton preguiçoso para evitar exceptions na ausência do client
_metrics: dict[str, Any] | None = None

# Guarda se o servidor HTTP de métricas já foi iniciado neste processo. O
# start_http_server do prometheus_client sobe um HTTPServer global em uma thread
# de background; chamá-lo duas vezes levantaria "Address already in use". Como o
# mcp_server.py pode criar múltiplos tenants via create_app(), o guard impede
# tentativas duplicadas (Bloco 13 / E8-T1 — Grafana).
_metrics_server_started: bool = False


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
                # ── Quality Gate RAGAS (Bloco 6 / E1-T2) ───────────────────
                # Gauges contínuos (0-1) por modo de avaliação efetivo
                # ("ragas" quando RAGAS real disponível, "proxy" no proxy
                # determinístico baseado em SynthesizedClaim, "timeout"/"error"
                # em falhas graciosas). Labels por modo permitem comparar
                # qualidade entre modos de operação no Grafana (Bloco 13).
                "ragas_faithfulness": Gauge(
                    "sra_ragas_faithfulness_score",
                    "Score de faithfulness do Quality Gate RAGAS (0-1)",
                    ["mode"],
                ),
                "ragas_relevancy": Gauge(
                    "sra_ragas_relevancy_score",
                    "Score de relevancy do Quality Gate RAGAS (0-1)",
                    ["mode"],
                ),
                "ragas_traceability": Gauge(
                    "sra_ragas_traceability_score",
                    "Cobertura de rastreabilidade das claims (0-1)",
                    ["mode"],
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
    """Inicia o servidor de escuta HTTP do Prometheus client em thread de background.

    Idempotente: chamadas repetidas (ex.: múltiplos tenants via ``create_app``)
    não reabrem a porta. Fail-open: qualquer erro é logado e engolido, para nunca
    quebrar o startup da aplicação SRA.
    """
    global _metrics_server_started
    if _metrics_server_started:
        logger.debug("Metrics Server já está ativo; ignorando chamada repetida.")
        return
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
        _metrics_server_started = True
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
