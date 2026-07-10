"""API REST corporativa em FastAPI para o Smart Research Agent.

.. note:: **Módulo legado / alternativo (Plano Parte 3 — Fase 1, §15.2).**
   O servidor oficial de produção é ``src/mcp_server.py`` (é o que o Dockerfile
   sobe e o que expõe as 15+ tools MCP). Este módulo permanece por
   compatibilidade e para uso standalone da API REST, mas suas rotas de
   pesquisa/agendamento/observabilidade agora vivem em ``rest_router``, que é
   reutilizado por ``src/mcp_server.py`` sob o prefixo ``/api/v2``. Prefira
   ``uvicorn src.mcp_server:app`` para produção.

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
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any, TYPE_CHECKING
from contextlib import asynccontextmanager

if TYPE_CHECKING:  # evita import circular em runtime
    from src.config import Config

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Request,
    Security,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from api.streaming import ProgressBroker, ProgressEvent, sse_headers
from src.config import config_manager, get_config

# Armazenamento em memoria das tarefas assíncronas (Thread-Safe)
_task_store: Dict[str, Dict[str, Any]] = {}
_task_lock = asyncio.Lock()

# Broker de progresso compartilhado por todas as rotas assíncronas/streaming.
_progress_broker = ProgressBroker()

# Tracker de custo por fonte/sessão (Fase 5 — Observabilidade). Instância única
# em memória compartilhada por todas as rotas que registram custo de busca.
from src.monitoring.budget_tracker import BudgetTracker

budget_tracker = BudgetTracker()

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida do FastAPI - setup e shutdown."""
    from src.config import Config
    from src.observability.metrics import start_metrics_server
    from src.utils.logging import setup_logging
    from src.monitoring.tracing import setup_tracing

    config = Config()
    config.validate_config()

    # Aviso de segurança: API sem autenticação (SRA_API_KEY ausente).
    if not config.sra_api_key:
        logger.warning(
            "SRA_API_KEY not configured. API is running without authentication. "
            "Set SRA_API_KEY in .env for production use."
        )

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

# Rate limiting por IP (Auditoria Parte 2 — Fase 3.3). Usa slowapi; a chave é
# o endereço remoto. Endpoints de pesquisa (custo alto em tokens/scraping)
# aplicam @limiter.limit. /health e /docs ficam isentos.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS lê a lista de origens permitidas do .env (CORS_ALLOWED_ORIGINS), em vez de
# estar hardcoded como "*". Default preserva o comportamento de dev local.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config_manager.config.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Autenticação da API (Auditoria Parte 2 — Fase 3.1) ────────────────
# Dependência que exige a X-API-Key do SRA nos endpoints de pesquisa. Se
# SRA_API_KEY não estiver configurada no .env, a autenticação é desabilitada
# (compatibilidade com uso local sem configuração).
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
    config: "Config" = Depends(get_config),
) -> None:
    """Verifica a API Key do SRA nos endpoints de pesquisa.

    Se ``SRA_API_KEY`` não estiver configurada no ``.env``, a autenticação é
    desabilitada (compatibilidade com uso local sem configuração) e a função
    retorna sem erro. Caso contrário, exige que o header ``X-API-Key`` traga
    exatamente o valor configurado.

    Args:
        api_key: Valor do header ``X-API-Key`` (ou ``None`` se ausente).
        config: Configuração efetiva do contexto (via ``get_config``).

    Raises:
        HTTPException: 401 se a chave estiver ausente ou incorreta.
    """
    if not config.sra_api_key:
        # Modo sem auth: o aviso já foi emitido no startup (uma vez).
        return
    if not api_key or api_key != config.sra_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. Use header: X-API-Key: <your-key>",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ─── Router REST compartilhado ────────────────────────────────
# As rotas de pesquisa/agendamento/observabilidade vivem em um APIRouter para
# que o servidor oficial (`src/mcp_server.py`) possa reutilizá-las via
# `app.include_router(rest_router, prefix="/api/v2")` sem duplicar código
# (Plano Parte 3 — Fase 1, §15.2). O `app` deste módulo (legado) inclui o
# mesmo router no final do arquivo, preservando os caminhos originais.
rest_router = APIRouter()


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


class ScheduleRequest(BaseModel):
    """Payload para agendar uma pesquisa recorrente (FASE 6)."""

    query: str = Field(..., min_length=3, max_length=500, example="Novidades em RAG")
    cron: str = Field("0 8 * * *", example="0 8 * * *")
    webhook_url: Optional[str] = Field(None, example="https://hooks.slack.com/...")
    output_dir: str = Field("reports/scheduled", example="reports/scheduled")
    alert_on_changes: bool = Field(True)


class ScheduleResponse(BaseModel):
    job_id: str
    query: str
    cron: str
    output_dir: str


# ─── Núcleo de execução compartilhado ──────────────────────────
def _build_response(
    req: ResearchRequest, synthesis: Any, duration: float
) -> ResearchResponse:
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
    from src.orchestrator_factory import create_orchestrator

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

        orchestrator = create_orchestrator(config)
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


@rest_router.post(
    "/api/research",
    response_model=ResearchResponse,
    status_code=201,
    dependencies=[Depends(verify_api_key)],
)
@limiter.limit("10/minute")
async def research_sync(request: Request, req: ResearchRequest):
    """Executa a pesquisa de forma síncrona, bloqueando a requisição até o relatório final.

    Recomendado apenas para modos rápidos ou integrações que não suportam SSE.
    Para pesquisas longas, prefira `/api/research/stream` ou `/api/research/async`.
    """
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator

    start_time = time.monotonic()
    try:
        config = Config()
        config.operation_mode = req.mode
        config.max_results_per_source = req.max_results

        orchestrator = create_orchestrator(config)
        result = await orchestrator.research(req.query)
        duration = time.monotonic() - start_time

        return _build_response(req, result, duration)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na pesquisa síncrona: {e}")


@rest_router.post(
    "/api/research/async",
    response_model=TaskAcceptedResponse,
    status_code=202,
    dependencies=[Depends(verify_api_key)],
)
@limiter.limit("10/minute")
async def research_async(
    request: Request, req: ResearchRequest, background_tasks: BackgroundTasks
):
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


@rest_router.get("/api/research/{task_id}", response_model=TaskStatusResponse)
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


@rest_router.post(
    "/api/research/stream",
    status_code=200,
    dependencies=[Depends(verify_api_key)],
)
@limiter.limit("10/minute")
async def research_stream(request: Request, req: ResearchRequest):
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


@rest_router.get("/api/research/{task_id}/stream")
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


@rest_router.get("/api/circuit-breakers")
async def get_circuit_breakers():
    """Retorna o status atualizado de todos os disjuntores da aplicação."""
    from src.utils.circuit_breaker import CircuitBreakerRegistry

    return CircuitBreakerRegistry.status_all()


@rest_router.get("/api/source-costs/{session_id}")
async def get_source_costs(session_id: str):
    """Retorna custo e performance por fonte de busca para uma sessão.

    Útil para observabilidade fina: identifica fontes lentas ou caras que
    podem estar degradando a experiência de uma pesquisa específica.
    """
    summary = budget_tracker.get_source_cost_summary(session_id)
    return {
        "session_id": session_id,
        "sources": summary,
        "total_requests": sum(s["requests"] for s in summary.values()),
        "slowest_source": max(
            summary, key=lambda k: summary[k]["avg_latency_ms"], default=None
        ),
    }


# ─── Agendamento de pesquisas recorrentes (FASE 6) ─────────────────────
def _build_scheduler() -> Any:
    """Constrói um ResearchScheduler com um Orchestrator próprio.

    Instanciado por requisição para não acoplar o ciclo de vida do scheduler
    (persistido em JSON no disco) ao processo da API.
    """
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator
    from src.scheduler import ResearchScheduler

    return ResearchScheduler(create_orchestrator(Config()))


@rest_router.post(
    "/api/schedule",
    response_model=ScheduleResponse,
    status_code=201,
    dependencies=[Depends(verify_api_key)],
)
async def schedule_research(payload: ScheduleRequest) -> ScheduleResponse:
    """Agenda uma pesquisa recorrente com detecção de mudanças e alertas.

    O job é persistido em ``reports/scheduled_jobs.json`` e pode ser executado
    por um worker externo (APScheduler ou cron) via ``run_scheduled_research``.
    """
    try:
        scheduler = _build_scheduler()
        job_id = scheduler.schedule_research(
            query=payload.query,
            cron_expr=payload.cron,
            output_dir=payload.output_dir,
            webhook_url=payload.webhook_url,
            alert_on_changes=payload.alert_on_changes,
        )
        return ScheduleResponse(
            job_id=job_id,
            query=payload.query,
            cron=payload.cron,
            output_dir=payload.output_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao agendar pesquisa: {e}")


@rest_router.get("/api/schedule", dependencies=[Depends(verify_api_key)])
async def list_scheduled_research() -> dict:
    """Lista todas as pesquisas recorrentes agendadas."""
    scheduler = _build_scheduler()
    jobs = scheduler.list_jobs()
    return {"total": len(jobs), "jobs": jobs}


@rest_router.delete("/api/schedule/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_scheduled_research(job_id: str) -> dict:
    """Cancela e remove uma pesquisa recorrente agendada."""
    scheduler = _build_scheduler()
    if not scheduler.cancel_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' não encontrado")
    return {"cancelled": True, "job_id": job_id}


# Inclui o router no app legado deste módulo, preservando os caminhos originais
# (ex.: POST /api/research). O servidor oficial `src/mcp_server.py` inclui o
# mesmo `rest_router` sob o prefixo `/api/v2`.
app.include_router(rest_router)
