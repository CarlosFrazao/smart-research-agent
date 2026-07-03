from __future__ import annotations
import asyncio
import logging
from typing import Optional

from celery import Celery

logger = logging.getLogger(__name__)


celery_app = Celery(
    "smart_research_agent",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3000,
    worker_prefetch_multiplier=1,
    result_expires=86400,
)


def _apply_config_overrides() -> None:
    """Sobrescreve broker/backend com valores do .env se disponiveis."""
    try:
        from src.config import Config
        cfg = Config()
        broker = getattr(cfg, "celery_broker_url", None)
        backend = getattr(cfg, "celery_result_backend", None)
        if broker:
            celery_app.conf.broker_url = broker
        if backend:
            celery_app.conf.result_backend = backend
    except Exception as e:
        logger.debug(f"celery_app: nao foi possivel carregar Config para override: {e}")


_apply_config_overrides()


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="sra.research",
)
def research_task(
    self,
    query: str,
    mode: str = "standard",
    options: Optional[dict] = None,
) -> dict:
    """
    Task Celery para processamento de pesquisas em background.

    Suporta até 2 retentativas com intervalo de 60s entre elas.
    Executa o Orchestrator.research() em um loop asyncio isolado
    para compatibilidade com ambientes multi-process do Celery.
    """
    try:
        from src.config import Config
        from src.orchestrator import Orchestrator

        cfg = Config()
        if mode and mode != "standard":
            cfg_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else {}
            cfg_dict["operation_mode"] = mode
            try:
                cfg = Config(**{k: v for k, v in cfg_dict.items() if v is not None})
            except Exception:
                pass

        orchestrator = Orchestrator(cfg)

        # Cria novo loop asyncio isolado — necessario em workers Celery (processo filho)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(orchestrator.research(query))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

        return {"status": "success", "query": query, "mode": mode, "result": result}

    except Exception as exc:
        logger.error(f"research_task falhou (tentativa {self.request.retries + 1}): {exc}")
        raise self.retry(exc=exc, countdown=60)