"""API REST corporativa em FastAPI para o Smart Research Agent.

Expõe três formas de consumir uma pesquisa, todas compartilhando a mesma
função de execução (`_run_research_job`) e o mesmo `Orchestrator.research()`:

1. Síncrona (`POST /api/research`): bloqueia até o relatório final. Simples,
   mas inadequada para pesquisas longas (podem levar minutos) atrás de
   load balancers com timeout curto.
2. Assíncrona com polling (`POST /api/research/async` + `GET /api/research/{id}`):
   não bloqueia, mas o cliente precisa ficar consultando o status.
3. Assíncrona com streaming (`POST /api/research/stream` e
   `GET /api/research/{id}/stream`): usa Server-Sent Events (SSE) para
   empurrar progresso em tempo real (etapa atual, percentual, mensagem) e o
   resultado final assim que ficam disponíveis — sem polling.

O transporte SSE (formatação de eventos, pub/sub, heartbeats) vive em
`api/streaming.py`, propositalmente desacoplado de `Orchestrator`/`Config`.
"""

import asyncio
import time
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.streaming import ProgressBroker, ProgressEvent, sse_headers

# Armazenamento em memoria das tarefas assíncronas (Thread-Safe)
_task_store: Dict[str, Dict[str, Any]] = {}
_task_lock = asyncio.Lock()

# Broker de progresso compartilhado por todas as rotas assíncronas/streaming.
_progress_broker = ProgressBroker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida do FastAPI - setup e shutdown."""
    from src.config import Config
    from src.observability.metrics import start_metrics_server
    from src.utils.logging import setup_logging
    from src.monitoring.tracing import setup_tracing

    config = Config()
    config.validate_config()

    # Inicia logs JSON e servidor Prometheus
    setup_logging(level=config.log_level, json_output=True)
    start_metrics_server(port=8001)

    # Inicia tracing distribuído (OpenTelemetry), se habilitado via .env.
    # Degrada para no-op automaticamente se opentelemetry-sdk não estiver instalado.
    if config.otel_enabled:
        setup_tracing(
            service_name=config.otel_service_name,
            otlp_endpoint=config.otel_exporter_otlp_endpoint,
            console_export=config.otel_console_export,
        )
    yield
    # Recursos limpos automaticamente


app = FastAPI(
    title="Smart Research Agent API",
    description="Interface RESTful de Pesquisa Inteligente",
    version="6.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Correlation ID por requisição: propaga/gera X-Request-ID, injeta nos logs e
# spans de tudo o que a requisição disparar downstream.
from src.monitoring.tracing import CorrelationIdMiddleware
app.add_middleware(CorrelationIdMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção deve ser restrito ao domínio da UI
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Schemas Pydantic ─────────────────────────────────────────
class ResearchRequest(BaseModel):
    query: str = Field(
        ..., min_length=3, max_length=500, example="Best databases in 2026"
    )
    mode: str = Field("cirurgia", example="guerrilha")
    languages: List[str] = Field(["en"], example=["en", "pt"])
    max_results: int = Field(10, ge=1, le=50)


class ResearchResponse(BaseModel):
    query: str
    mode: str
    synthesis: str
    duration_seconds: float
    timestamp: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: Optional[ProgressEvent] = None
    result: Optional[ResearchResponse] = None
    error: Optional[str] = None


class TaskAcceptedResponse(BaseModel):
    task_id: str
    status: str
    poll_url: str
    stream_url: str


# ─── Núcleo de execução compartilhado ──────────────────────────
def _build_response(req: ResearchRequest, synthesis: Any, duration: float) -> ResearchResponse:
    """Monta o payload de resposta padrão a partir do resultado do Orchestrator."""
    return ResearchResponse(
        query=req.query,
        mode=req.mode,
        synthesis=synthesis if isinstance(synthesis, str) else str(synthesis),
        duration_seconds=round(duration, 2),
        timestamp=datetime.utcnow().isoformat(),
    )


async def _run_research_job(task_id: str, req: ResearchRequest) -> None:
    """Executa uma pesquisa completa, publicando progresso e persistindo o status final.

    Usado tanto pelo fluxo de polling (`/api/research/async`) quanto pelo de
    streaming (`/api/research/stream`), garantindo que ambos vejam exatamente
    o mesmo comportamento e as mesmas etapas de progresso.
    """
    from src.config import Config
    from src.orchestrator import Orchestrator

    async with _task_lock:
        _task_store[task_id] = {"status": "running", "progress": None}

    def on_progress(step: int, total_steps: int, message: str) -> None:
        event = ProgressEvent.progress(task_id, step, total_steps, message)
        _progress_broker.publish(task_id, event)
        # Permite que clientes que apenas fazem polling também vejam o progresso.
        _task_store.setdefault(task_id, {})["progress"] = event

    try:
        start_job = time.monotonic()
        config = Config()
        config.operation_mode = req.mode
        config.max_results_per_source = req.max_results

        orchestrator = Orchestrator(config)
        result = await orchestrator.research(req.query, progress_callback=on_progress)
        duration = time.monotonic() - start_job

        response = _build_response(req, result, duration)
        async with _task_lock:
            _task_store[task_id]["status"] = "done"
            _task_store[task_id]["result"] = response

        _progress_broker.publish(
            task_id, ProgressEvent.result(task_id, response.model_dump())
        )
    except Exception as e:
        async with _task_lock:
            _task_store.setdefault(task_id, {})["status"] = "error"
            _task_store[task_id]["error"] = str(e)
        _progress_broker.publish(task_id, ProgressEvent.failure(task_id, str(e)))


# ─── Rotas API ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "6.1.0",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/research", response_model=ResearchResponse, status_code=201)
async def research_sync(req: ResearchRequest):
    """Executa a pesquisa de forma síncrona, bloqueando a requisição até o relatório final.

    Recomendado apenas para modos rápidos ou integrações que não suportam SSE.
    Para pesquisas longas, prefira `/api/research/stream` ou `/api/research/async`.
    """
    from src.config import Config
    from src.orchestrator import Orchestrator

    start_time = time.monotonic()
    try:
        config = Config()
        config.operation_mode = req.mode
        config.max_results_per_source = req.max_results

        orchestrator = Orchestrator(config)
        result = await orchestrator.research(req.query)
        duration = time.monotonic() - start_time

        return _build_response(req, result, duration)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na pesquisa síncrona: {e}")


@app.post("/api/research/async", response_model=TaskAcceptedResponse, status_code=202)
async def research_async(req: ResearchRequest, background_tasks: BackgroundTasks):
    """Inicia a pesquisa em background e retorna imediatamente um token (task_id).

    O progresso e o resultado podem ser consultados via polling
    (`GET /api/research/{task_id}`) ou, preferencialmente, via streaming
    (`GET /api/research/{task_id}/stream`).
    """
    task_id = str(uuid.uuid4())[:8]
    _progress_broker.open(task_id)
    async with _task_lock:
        _task_store[task_id] = {"status": "queued", "progress": None}

    background_tasks.add_task(_run_research_job, task_id, req)

    return TaskAcceptedResponse(
        task_id=task_id,
        status="queued",
        poll_url=f"/api/research/{task_id}",
        stream_url=f"/api/research/{task_id}/stream",
    )


@app.get("/api/research/{task_id}", response_model=TaskStatusResponse)
async def get_research_status(task_id: str):
    """Consulta o progresso e resultado final de um processamento assíncrono (polling)."""
    async with _task_lock:
        task = _task_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    return TaskStatusResponse(
        task_id=task_id,
        status=task.get("status", "unknown"),
        progress=task.get("progress"),
        result=task.get("result"),
        error=task.get("error"),
    )


@app.post("/api/research/stream", status_code=200)
async def research_stream(req: ResearchRequest):
    """Executa a pesquisa e transmite progresso + resultado em tempo real via SSE.

    Formato dos eventos (um por linha `data:`, no padrão EventSource):
    - `event: progress` — `{step, total_steps, percent, message}` a cada etapa do pipeline.
    - `event: result`   — payload final equivalente a `ResearchResponse`.
    - `event: error`    — mensagem de erro, caso a pesquisa falhe.
    - `event: heartbeat`— comentário de keep-alive (a cada ~15s sem novo progresso).
    """
    task_id = str(uuid.uuid4())[:8]
    _progress_broker.open(task_id)
    async with _task_lock:
        _task_store[task_id] = {"status": "running", "progress": None}

    # Importante: NÃO usar BackgroundTasks aqui. BackgroundTasks só executam
    # depois que a resposta termina de ser enviada, mas uma StreamingResponse
    # só "termina" quando o gerador para de produzir eventos — e o gerador
    # depende justamente dos eventos publicados por esta tarefa. Usar
    # BackgroundTasks causaria deadlock (stream nunca recebe nada).
    asyncio.create_task(_run_research_job(task_id, req))

    return StreamingResponse(
        _progress_broker.event_stream(task_id),
        media_type="text/event-stream",
        headers={**sse_headers(), "X-Task-Id": task_id},
    )


@app.get("/api/research/{task_id}/stream")
async def research_stream_reconnect(task_id: str):
    """Assina (ou reassina) o stream de progresso de uma tarefa já iniciada.

    Útil para reconectar após queda de conexão: eventos já publicados são
    reenviados (replay) antes de continuar com eventos ao vivo. Funciona tanto
    para tarefas criadas via `/api/research/async` quanto via `/api/research/stream`.
    """
    if not _progress_broker.exists(task_id):
        async with _task_lock:
            known = task_id in _task_store
        if not known:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    return StreamingResponse(
        _progress_broker.event_stream(task_id),
        media_type="text/event-stream",
        headers={**sse_headers(), "X-Task-Id": task_id},
    )


@app.get("/api/circuit-breakers")
async def get_circuit_breakers():
    """Retorna o status atualizado de todos os disjuntores da aplicação."""
    from src.utils.circuit_breaker import CircuitBreakerRegistry

    return CircuitBreakerRegistry.status_all()
