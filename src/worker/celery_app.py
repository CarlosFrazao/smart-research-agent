from __future__ import annotations
import asyncio
import logging
from typing import Optional, Any
from celery import Celery
from src.config import Config

logger = logging.getLogger(__name__)

# Instancia a config para ler parametros de conexao do Celery
_config = Config()

celery_app = Celery(
    "smart_research_agent",
    broker=_config.celery_broker_url,
    backend=_config.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,          # 1 hora maximo
    task_soft_time_limit=3000,     # Alerta aos 50min
    worker_prefetch_multiplier=1,  # Um task por vez
    result_expires=86400,          # 24 horas
)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="sra.research",
)
def research_task(self, query: str, mode: str = "standard", options: Optional[dict] = None) -> dict:
    """Task de pesquisa processada em background via Celery."""
    from src.config import Config as SRAConfig
    from src.orchestrator import Orchestrator

    try:
        config = SRAConfig()
        if mode and mode != "standard":
            config.operation_mode = mode
        
        # Copia opcoes customizadas se fornecidas
        if options:
            for k, v in options.items():
                if hasattr(config, k):
                    setattr(config, k, v)

        orchestrator = Orchestrator(config)
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(
            orchestrator.research(query)
        )
        return {"status": "success", "result": result, "query": query, "mode": mode}
    except Exception as exc:
        logger.error(f"research_task falhou: {exc}")
        # Tenta retry se for um erro transitorio
        try:
            raise self.retry(exc=exc, countdown=60)
        except Exception as retry_exc:
            # Se exceder o limite de retries, levanta a excecao original
            raise retry_exc