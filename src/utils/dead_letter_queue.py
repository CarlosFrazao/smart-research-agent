"""
Dead Letter Queue (DLQ) — Armazena tarefas que falharam permanentemente
para análise posterior ou retry controlado.

Cada tarefa é persistida como JSON em disco em um diretório configurável (.dlq/).
"""
import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass
class FailedTask:
    task_id: str
    task_type: str      # "search", "llm_call", "scrape"
    payload: dict
    error: str
    timestamp: str
    retry_count: int = 0
    source: str = ""


class DeadLetterQueue:
    """
    Persiste tarefas falhas em disco (JSON) e oferece mecanismo de retry.
    Thread-safe para uso assíncrono via asyncio.
    """

    MAX_RETRIES = 3

    def __init__(self, path: str = "./.dlq"):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"DeadLetterQueue iniciada em: {self.path.resolve()}")

    # ------------------------------------------------------------------
    # Criação de tarefas
    # ------------------------------------------------------------------

    def create_failed_task(
        self,
        task_type: str,
        payload: dict,
        error: str,
        source: str = "",
    ) -> FailedTask:
        """Cria um FailedTask com ID único e timestamp ISO."""
        return FailedTask(
            task_id=str(uuid.uuid4())[:8],
            task_type=task_type,
            payload=payload,
            error=str(error),
            timestamp=datetime.now().isoformat(),
            source=source,
        )

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    async def push(self, task: FailedTask) -> None:
        """Persiste uma tarefa falha como arquivo JSON no disco."""
        filename = f"{task.task_id}_{int(datetime.now().timestamp())}.json"
        filepath = self.path / filename
        try:
            filepath.write_text(
                json.dumps(asdict(task), indent=2, default=str),
                encoding="utf-8",
            )
            logger.info(
                f"DLQ: task {task.task_id} ({task.task_type}) arquivada "
                f"[source={task.source or 'unknown'}]"
            )
        except Exception as e:
            logger.error(f"DLQ: falha ao persistir task {task.task_id}: {e}")

    async def pop_all(self) -> list[FailedTask]:
        """Lê e remove todas as tarefas da fila do disco."""
        tasks = []
        for file in sorted(self.path.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                tasks.append(FailedTask(**data))
                file.unlink()
            except Exception as e:
                logger.error(f"DLQ: erro ao ler/remover {file.name}: {e}")
        return tasks

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    async def retry_all(self, handler: Callable) -> dict:
        """
        Reprocessa todas as tarefas da DLQ com o handler fornecido.
        Tarefas que falham novamente são re-enfileiradas até MAX_RETRIES.
        Após MAX_RETRIES, são descartadas e contadas como permanent_fail.

        Returns:
            dict com 'success', 'requeued' e 'permanent_fail'
        """
        tasks = await self.pop_all()
        success = 0
        requeued = 0
        permanent_fail = 0

        for task in tasks:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(task)
                else:
                    handler(task)
                success += 1
                logger.info(f"DLQ retry OK: task {task.task_id} ({task.task_type})")
            except Exception as e:
                task.retry_count += 1
                task.error = str(e)
                if task.retry_count < self.MAX_RETRIES:
                    await self.push(task)
                    requeued += 1
                    logger.warning(
                        f"DLQ: retry {task.retry_count}/{self.MAX_RETRIES} "
                        f"para task {task.task_id}: {e}"
                    )
                else:
                    permanent_fail += 1
                    logger.error(
                        f"DLQ: task {task.task_id} falhou permanentemente "
                        f"após {task.retry_count} tentativas: {e}"
                    )

        logger.info(
            f"DLQ retry_all: success={success}, requeued={requeued}, "
            f"permanent_fail={permanent_fail}"
        )
        return {"success": success, "requeued": requeued, "permanent_fail": permanent_fail}

    def size(self) -> int:
        """Retorna o número de tarefas atualmente na fila."""
        return len(list(self.path.glob("*.json")))
