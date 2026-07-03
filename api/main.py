"""API REST corporativa em FastAPI para o Smart Research Agent."""
import asyncio
import time
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Armazenamento em memoria das tarefas assíncronas (Thread-Safe)
_task_store: Dict[str, Dict[str, Any]] = {}
_task_lock = asyncio.Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida do FastAPI - setup e shutdown."""
    from src.config import Config
    from src.observability.metrics import start_metrics_server
    from src.utils.logging import setup_logging
    
    config = Config()
    config.validate_config()
    
    # Inicia logs JSON e servidor Prometheus
    setup_logging(level=config.log_level, json_output=True)
    start_metrics_server(port=8001)
    yield
    # Recursos limpos automaticamente

app = FastAPI(
    title="Smart Research Agent API",
    description="Interface RESTful de Pesquisa Inteligente",
    version="6.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção deve ser restrito ao domínio da UI
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Schemas Pydantic ─────────────────────────────────────────
class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=500, example="Best databases in 2026")
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
    result: Optional[ResearchResponse] = None
    error: Optional[str] = None

# ─── Rotas API ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "6.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/api/research", response_model=ResearchResponse, status_code=201)
async def research_sync(req: ResearchRequest):
    """Executa a pesquisa de forma síncrona, bloqueando a requisição até o relatório final."""
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
        
        return ResearchResponse(
            query=req.query,
            mode=req.mode,
            synthesis=result if isinstance(result, str) else str(result),
            duration_seconds=round(duration, 2),
            timestamp=datetime.utcnow().isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na pesquisa síncrona: {e}")

@app.post("/api/research/async", status_code=202)
async def research_async(req: ResearchRequest, background_tasks: BackgroundTasks):
    """Inicia a pesquisa em background e retorna imediatamente um token (task_id) para consulta."""
    task_id = str(uuid.uuid4())[:8]
    async with _task_lock:
        _task_store[task_id] = {"status": "queued"}

    async def run_research_job():
        from src.config import Config
        from src.orchestrator import Orchestrator
        
        async with _task_lock:
            _task_store[task_id]["status"] = "running"
        try:
            start_job = time.monotonic()
            config = Config()
            config.operation_mode = req.mode
            config.max_results_per_source = req.max_results
            
            orchestrator = Orchestrator(config)
            result = await orchestrator.research(req.query)
            duration = time.monotonic() - start_job
            
            res_payload = ResearchResponse(
                query=req.query,
                mode=req.mode,
                synthesis=result if isinstance(result, str) else str(result),
                duration_seconds=round(duration, 2),
                timestamp=datetime.utcnow().isoformat()
            )
            async with _task_lock:
                _task_store[task_id] = {
                    "status": "done",
                    "result": res_payload
                }
        except Exception as e:
            async with _task_lock:
                _task_store[task_id] = {
                    "status": "error",
                    "error": str(e)
                }

    background_tasks.add_task(run_research_job)
    return {
        "task_id": task_id,
        "status": "queued",
        "poll_url": f"/api/research/{task_id}"
    }

@app.get("/api/research/{task_id}", response_model=TaskStatusResponse)
async def get_research_status(task_id: str):
    """Consulta o progresso e resultado final de um processamento assíncrono."""
    async with _task_lock:
        task = _task_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    
    return TaskStatusResponse(
        task_id=task_id,
        status=task.get("status", "unknown"),
        result=task.get("result"),
        error=task.get("error")
    )

@app.get("/api/circuit-breakers")
async def get_circuit_breakers():
    """Retorna o status atualizado de todos os disjuntores da aplicação."""
    from src.utils.circuit_breaker import CircuitBreakerRegistry
    return CircuitBreakerRegistry.status_all()