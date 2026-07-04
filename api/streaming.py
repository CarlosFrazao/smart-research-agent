"""Infraestrutura de streaming via Server-Sent Events (SSE) para a API do SRA.

Este módulo é intencionalmente agnóstico de domínio: ele não sabe nada sobre
`Orchestrator`, `Config` ou pesquisa. Sua única responsabilidade é permitir que
qualquer tarefa assíncrona de longa duração publique eventos de progresso e que
um ou mais clientes HTTP os consumam em tempo real via SSE, com suporte a:

- Múltiplos assinantes por tarefa (fan-out), incluindo reconexão tardia
  (replay do histórico de eventos já publicados).
- Heartbeats periódicos para manter a conexão viva atrás de proxies/load
  balancers que fecham conexões ociosas.
- Encerramento limpo quando um evento terminal (`result` ou `error`) é
  publicado.

Uso típico (ver `api/main.py`):

    broker = ProgressBroker()
    broker.open(task_id)

    async def on_progress(step, total, message):
        broker.publish(task_id, ProgressEvent(
            task_id=task_id, event="progress",
            step=step, total_steps=total, message=message,
        ))

    await orchestrator.research(query, progress_callback=on_progress)
    broker.publish(task_id, ProgressEvent(task_id=task_id, event="result", data={...}))

    # Na rota FastAPI:
    return StreamingResponse(
        broker.event_stream(task_id), media_type="text/event-stream"
    )
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

# ─── Modelos ──────────────────────────────────────────────────────────────

EventType = Literal["progress", "result", "error", "heartbeat"]

# Eventos que encerram o stream para os assinantes (nenhum evento futuro é esperado).
TERMINAL_EVENTS: frozenset[str] = frozenset({"result", "error"})


class ProgressEvent(BaseModel):
    """Representa um único evento de progresso/resultado de uma tarefa assíncrona."""

    task_id: str
    event: EventType = "progress"
    step: int = 0
    total_steps: int = 0
    message: str = ""
    percent: float = Field(0.0, ge=0.0, le=100.0)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @classmethod
    def progress(cls, task_id: str, step: int, total_steps: int, message: str) -> "ProgressEvent":
        """Cria um evento de progresso calculando automaticamente o percentual."""
        percent = round((step / total_steps) * 100, 1) if total_steps > 0 else 0.0
        percent = max(0.0, min(100.0, percent))
        return cls(
            task_id=task_id,
            event="progress",
            step=step,
            total_steps=total_steps,
            message=message,
            percent=percent,
        )

    @classmethod
    def result(cls, task_id: str, data: Dict[str, Any], total_steps: int = 0) -> "ProgressEvent":
        """Cria o evento terminal de sucesso, carregando o payload final."""
        return cls(
            task_id=task_id,
            event="result",
            step=total_steps,
            total_steps=total_steps,
            message="Concluído",
            percent=100.0,
            data=data,
        )

    @classmethod
    def failure(cls, task_id: str, error: str) -> "ProgressEvent":
        """Cria o evento terminal de erro."""
        return cls(task_id=task_id, event="error", message="Falha na tarefa", error=error)


# Assinatura compatível com `Orchestrator.research(progress_callback=...)`.
OrchestratorProgressCallback = Callable[[int, int, str], Union[None, Awaitable[None]]]


def format_sse(event: ProgressEvent) -> str:
    """Serializa um `ProgressEvent` no formato de wire do SSE.

    Inclui `id:` (útil para `Last-Event-ID` em reconexões automáticas do
    EventSource do navegador) e `event:` (permite ao cliente usar
    `addEventListener('progress', ...)`, `addEventListener('result', ...)` etc).
    """
    payload = event.model_dump_json()
    event_id = f"{event.task_id}-{event.step}-{int(time.time() * 1000)}"
    return f"id: {event_id}\nevent: {event.event}\ndata: {payload}\n\n"


def format_sse_comment(text: str = "keep-alive") -> str:
    """Formata um comentário SSE (linha iniciada com `:`), usado como heartbeat.

    Comentários são ignorados pelo cliente mas mantêm a conexão TCP/HTTP viva.
    """
    return f": {text}\n\n"


class _TaskChannel:
    """Estado interno de uma tarefa: histórico de eventos + assinantes ativos."""

    __slots__ = ("history", "subscribers", "finished", "created_at", "finished_at")

    def __init__(self) -> None:
        self.history: List[ProgressEvent] = []
        self.subscribers: List["asyncio.Queue[Optional[ProgressEvent]]"] = []
        self.finished: bool = False
        self.created_at: float = time.monotonic()
        self.finished_at: Optional[float] = None


class ProgressBroker:
    """Broker de pub/sub em memória para eventos de progresso, por `task_id`.

    Thread-safety: pensado para uso dentro de um único processo asyncio (o
    mesmo modelo já usado pelo `_task_store` da API). Para múltiplos workers/
    processos seria necessário um backend compartilhado (Redis pub/sub, etc.),
    fora do escopo desta refatoração.
    """

    def __init__(self, retention_seconds: float = 900.0) -> None:
        self._channels: Dict[str, _TaskChannel] = {}
        self._lock = asyncio.Lock()
        self._retention_seconds = retention_seconds

    def open(self, task_id: str) -> None:
        """Cria (idempotentemente) o canal de uma tarefa antes dela iniciar."""
        if task_id not in self._channels:
            self._channels[task_id] = _TaskChannel()

    def exists(self, task_id: str) -> bool:
        return task_id in self._channels

    def publish(self, task_id: str, event: ProgressEvent) -> None:
        """Publica um evento para todos os assinantes atuais e no histórico.

        Seguro de chamar mesmo se nenhum assinante estiver conectado ainda
        (o evento fica disponível via replay para quem conectar depois).
        """
        channel = self._channels.setdefault(task_id, _TaskChannel())
        channel.history.append(event)
        if event.event in TERMINAL_EVENTS:
            channel.finished = True
            channel.finished_at = time.monotonic()

        for queue in list(channel.subscribers):
            queue.put_nowait(event)

        self._gc()

    async def subscribe(
        self,
        task_id: str,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[ProgressEvent]:
        """Assina os eventos de uma tarefa, com replay do histórico e heartbeats.

        Encerra automaticamente logo após entregar um evento terminal
        (`result` ou `error`), ou se o canal nunca existiu.
        """
        channel = self._channels.setdefault(task_id, _TaskChannel())

        # Replay: entrega tudo que já aconteceu antes desta conexão existir.
        for past_event in list(channel.history):
            yield past_event
            if past_event.event in TERMINAL_EVENTS:
                return

        queue: "asyncio.Queue[Optional[ProgressEvent]]" = asyncio.Queue()
        channel.subscribers.append(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                except asyncio.TimeoutError:
                    yield ProgressEvent(task_id=task_id, event="heartbeat")
                    continue

                yield event
                if event.event in TERMINAL_EVENTS:
                    return
        finally:
            if queue in channel.subscribers:
                channel.subscribers.remove(queue)

    async def event_stream(
        self,
        task_id: str,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[str]:
        """Gera o corpo de resposta SSE pronto para `StreamingResponse`."""
        async for event in self.subscribe(task_id, heartbeat_interval=heartbeat_interval):
            if event.event == "heartbeat":
                yield format_sse_comment()
            else:
                yield format_sse(event)

    def close(self, task_id: str) -> None:
        """Remove o canal imediatamente (uso em testes ou limpeza manual)."""
        self._channels.pop(task_id, None)

    def _gc(self) -> None:
        """Remove canais finalizados há mais de `retention_seconds`.

        Chamado oportunisticamente a cada `publish`; evita crescimento
        ilimitado de memória em processos de longa duração sem exigir um
        scheduler dedicado.
        """
        now = time.monotonic()
        expired = [
            tid
            for tid, ch in self._channels.items()
            if ch.finished
            and ch.finished_at is not None
            and (now - ch.finished_at) > self._retention_seconds
            and not ch.subscribers
        ]
        for tid in expired:
            self._channels.pop(tid, None)


def make_progress_callback(
    broker: ProgressBroker, task_id: str
) -> OrchestratorProgressCallback:
    """Cria um callback compatível com `Orchestrator.research(progress_callback=...)`.

    Isola a camada de API do formato exato do callback do Orchestrator: se a
    assinatura mudar, apenas este adaptador precisa ser ajustado.
    """

    def _callback(step: int, total_steps: int, message: str) -> None:
        broker.publish(task_id, ProgressEvent.progress(task_id, step, total_steps, message))

    return _callback


def sse_headers() -> Dict[str, str]:
    """Cabeçalhos HTTP recomendados para respostas `text/event-stream`.

    - `Cache-Control: no-cache`: evita que proxies/CDNs armazenem o stream.
    - `X-Accel-Buffering: no`: desativa o buffering do Nginx, que do contrário
      atrasaria a entrega dos eventos até o buffer encher.
    - `Connection: keep-alive`: sinaliza explicitamente para não fechar cedo.
    """
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def dumps_compact(obj: Any) -> str:
    """Helper de serialização JSON compacta, usado onde `pydantic` não se aplica."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
