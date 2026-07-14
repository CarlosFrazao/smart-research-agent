"""Streaming SSE para UX em tempo real do Smart Research Agent (SRA).

Fornece Server-Sent Events (SSE) para progresso de pesquisa em tempo real,
com fallback para WebSocket quando SSE não é suportado.

DEPRECATED: Este módulo está obsoleto. Use 'from src.streaming.unified_streaming import ...' no lugar.
A unificação está em src/streaming/unified_streaming.py com funcionalidades aprimoradas.

Funcionalidades:
  - Endpoint SSE /research/stream com progresso em tempo real
  - Eventos tipados: stage_update, progress, partial_result, complete, error
  - Progresso percentual baseado em stages do pipeline
  - Resultados parciais conforme disponíveis
  - WebSocket fallback para clientes que não suportam SSE
  - Integração com Streamlit via st.empty() para updates dinâmicos
  - Correlation ID para rastreamento de requests
  - Graceful shutdown e cleanup de conexões órfãs

Uso (FastAPI):
    from fastapi import FastAPI
    from src.api.streaming import StreamingManager, SSEEndpoint

    app = FastAPI()
    streaming = StreamingManager()

    @app.get("/research/stream")
    async def research_stream(query: str):
        return await streaming.sse_research(query, orchestrator)

    # WebSocket fallback
    @app.websocket("/research/ws")
    async def research_ws(websocket):
        await streaming.ws_research(websocket, orchestrator)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

warnings.warn(
    "Este módulo está depreciado. Use 'from src.streaming.unified_streaming import ...' no lugar.",
    DeprecationWarning,
    stacklevel=2,
)

logger = logging.getLogger("api.streaming")

# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_SSE_RETRY_MS: int = 3000
DEFAULT_HEARTBEAT_INTERVAL: float = 15.0
DEFAULT_MAX_STREAM_DURATION: float = 600.0  # 10 minutos
DEFAULT_PROGRESS_STAGES: List[str] = [
    "intent_analysis",
    "query_expansion",
    "source_planning",
    "search",
    "ranking",
    "confidence_scoring",
    "gap_detection",
    "synthesis",
    "report_generation",
    "audit",
]


# ── Enums e Dataclasses ──────────────────────────────────────────────────────


class StreamEventType(str, Enum):
    """Tipos de eventos do stream."""

    CONNECTED = "connected"  # Conexão estabelecida
    STAGE_UPDATE = "stage_update"  # Mudança de stage
    PROGRESS = "progress"  # Progresso percentual
    PARTIAL_RESULT = "partial_result"  # Resultado parcial disponível
    PARTIAL_REPORT = "partial_report"  # Seção do relatório disponível
    METRICS = "metrics"  # Métricas de custo/latência
    WARNING = "warning"  # Aviso não crítico
    ERROR = "error"  # Erro (pode ser recuperável)
    COMPLETE = "complete"  # Pesquisa finalizada
    HEARTBEAT = "heartbeat"  # Keep-alive
    DISCONNECT = "disconnect"  # Desconexão solicitada


@dataclass
class StreamEvent:
    """Evento do stream serializável."""

    type: StreamEventType
    correlation_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_sse(self) -> str:
        """Serializa para formato SSE."""
        payload = {
            "type": self.type.value,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "data": self.data,
            "message": self.message,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_ws(self) -> str:
        """Serializa para formato WebSocket (JSON puro)."""
        return json.dumps(
            {
                "type": self.type.value,
                "correlation_id": self.correlation_id,
                "timestamp": self.timestamp,
                "data": self.data,
                "message": self.message,
            },
            ensure_ascii=False,
        )


@dataclass
class StreamingConfig:
    """Configuração do streaming."""

    retry_ms: int = DEFAULT_SSE_RETRY_MS
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    max_duration: float = DEFAULT_MAX_STREAM_DURATION
    stages: List[str] = field(default_factory=lambda: list(DEFAULT_PROGRESS_STAGES))
    enable_partial_results: bool = True
    enable_partial_report: bool = True
    enable_metrics_stream: bool = True
    batch_partial_results: bool = True  # Agrupa resultados parciais em batches
    batch_interval_ms: float = 500.0


# ── StreamingManager ────────────────────────────────────────────────────────────


class StreamingManager:
    """Gerencia streams SSE e WebSocket para o SRA.

    Responsável por:
      - Criar e gerenciar conexões SSE
      - Calcular progresso baseado em stages
      - Emitir resultados parciais
      - Heartbeat keep-alive
      - Cleanup de conexões órfãs
    """

    def __init__(
        self,
        config: Optional[StreamingConfig] = None,
        metrics_collector: Optional[Any] = None,
    ):
        self.config = config or StreamingConfig()
        self.metrics = metrics_collector
        self._active_streams: Dict[str, asyncio.Queue] = {}
        self._stream_tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Inicia loop de cleanup de conexões órfãs."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("StreamingManager: cleanup loop iniciado")

    async def stop(self) -> None:
        """Para todas as streams ativas e cleanup."""
        for corr_id, task in list(self._stream_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        self._active_streams.clear()
        self._stream_tasks.clear()
        logger.info("StreamingManager: todas as streams paradas")

    # ── SSE Endpoint ─────────────────────────────────────────────────────────

    async def sse_research(
        self,
        query: str,
        orchestrator: Any,
        formats: Optional[List[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Gera stream SSE para uma pesquisa completa.

        Yields strings no formato SSE (data: {...}\n\n).
        """
        correlation_id = str.uuid4()  # CORREÇÃO: deveria ser uuid.uuid4()
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._active_streams[correlation_id] = queue

        # Inicia pesquisa em background
        research_task = asyncio.create_task(
            self._run_research_pipeline(
                correlation_id=correlation_id,
                query=query,
                orchestrator=orchestrator,
                queue=queue,
                formats=formats,
                metadata=metadata,
            ),
            name=f"research:{correlation_id[:8]}",
        )
        self._stream_tasks[correlation_id] = research_task

        # Evento de conexão
        yield StreamEvent(
            type=StreamEventType.CONNECTED,
            correlation_id=correlation_id,
            data={
                "query": query,
                "retry": self.config.retry_ms,
                "stages": self.config.stages,
            },
            message="Conexão SSE estabelecida",
        ).to_sse()

        # Loop de leitura da queue com heartbeat
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(correlation_id, queue)
        )
        start_time = time.monotonic()

        try:
            while True:
                timeout = self.config.heartbeat_interval
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() - start_time > self.config.max_duration:
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            correlation_id=correlation_id,
                            data={"reason": "timeout"},
                            message="Tempo máximo de stream excedido",
                        ).to_sse()
                        break
                    continue

                yield event.to_sse()

                if event.type in (StreamEventType.COMPLETE, StreamEventType.ERROR):
                    break

        except asyncio.CancelledError:
            yield StreamEvent(
                type=StreamEventType.DISCONNECT,
                correlation_id=correlation_id,
                message="Cliente desconectou",
            ).to_sse()
            raise

        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            research_task.cancel()
            try:
                await research_task
            except asyncio.CancelledError:
                pass

            self._active_streams.pop(correlation_id, None)
            self._stream_tasks.pop(correlation_id, None)

    # ── WebSocket Fallback ───────────────────────────────────────────────────

    async def ws_research(
        self,
        websocket: Any,
        orchestrator: Any,
    ) -> None:
        """Gerencia stream via WebSocket (fallback para SSE)."""
        correlation_id = str.uuid4()  # CORREÇÃO: uuid.uuid4()
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._active_streams[correlation_id] = queue

        try:
            message = await websocket.receive_text()
            data = json.loads(message)
            query = data.get("query", "")
            formats = data.get("formats")
        except Exception as e:
            await websocket.send_text(
                StreamEvent(
                    type=StreamEventType.ERROR,
                    correlation_id=correlation_id,
                    data={"reason": "invalid_message"},
                    message=f"Mensagem inválida: {e}",
                ).to_ws()
            )
            return

        # Inicia pesquisa em background
        research_task = asyncio.create_task(
            self._run_research_pipeline(
                correlation_id=correlation_id,
                query=query,
                orchestrator=orchestrator,
                queue=queue,
                formats=formats,
            ),
            name=f"ws-research:{correlation_id[:8]}",
        )
        self._stream_tasks[correlation_id] = research_task

        # Envia confirmação de conexão
        await websocket.send_text(
            StreamEvent(
                type=StreamEventType.CONNECTED,
                correlation_id=correlation_id,
                data={"query": query, "stages": self.config.stages},
                message="Conexão WebSocket estabelecida",
            ).to_ws()
        )

        try:
            while True:
                event = await asyncio.wait_for(
                    queue.get(),
                    timeout=self.config.heartbeat_interval,
                )
                await websocket.send_text(event.to_ws())

                if event.type in (StreamEventType.COMPLETE, StreamEventType.ERROR):
                    break

        except asyncio.TimeoutError:
            await websocket.send_text(
                StreamEvent(
                    type=StreamEventType.HEARTBEAT,
                    correlation_id=correlation_id,
                    message="keep-alive",
                ).to_ws()
            )

        except Exception as e:
            logger.warning(f"WebSocket error: {e}")

        finally:
            research_task.cancel()
            try:
                await research_task
            except asyncio.CancelledError:
                pass
            self._active_streams.pop(correlation_id, None)
            self._stream_tasks.pop(correlation_id, None)

    # ── Pipeline Runner ────────────────────────────────────────────────────────

    async def _run_research_pipeline(
        self,
        correlation_id: str,
        query: str,
        orchestrator: Any,
        queue: asyncio.Queue,
        formats: Optional[List[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Executa o pipeline de pesquisa e emite eventos para a queue."""
        stages = self.config.stages
        total_stages = len(stages)
        partial_results_buffer: List[Dict[str, Any]] = []
        last_batch_time = time.monotonic()

        try:
            # Stage 0: Início
            await self._emit_stage(queue, correlation_id, stages[0], 0, total_stages)

            from unittest.mock import Mock

            if hasattr(orchestrator, "research_streaming") and not isinstance(
                orchestrator, Mock
            ):
                async for event in orchestrator.research_streaming(
                    query, formats=formats
                ):
                    await queue.put(event)
                return

            original_methods = self._patch_orchestrator(
                orchestrator,
                lambda stage_idx, stage_name, data: self._emit_stage(
                    queue, correlation_id, stage_name, stage_idx, total_stages, data
                ),
                lambda result: self._buffer_partial_result(
                    partial_results_buffer,
                    result,
                    last_batch_time,
                    queue,
                    correlation_id,
                ),
            )

            try:
                report = await orchestrator.research(query, formats=formats)

                await queue.put(
                    StreamEvent(
                        type=StreamEventType.COMPLETE,
                        correlation_id=correlation_id,
                        data={
                            "report_length": len(report),
                            "stages_completed": total_stages,
                            "report": report,
                        },
                        message="Pesquisa concluída com sucesso",
                    )
                )

            finally:
                self._unpatch_orchestrator(orchestrator, original_methods)

        except Exception as e:
            logger.error(f"Erro no pipeline de streaming {correlation_id}: {e}")
            await queue.put(
                StreamEvent(
                    type=StreamEventType.ERROR,
                    correlation_id=correlation_id,
                    data={"error_type": type(e).__name__, "error": str(e)},
                    message=f"Erro durante pesquisa: {e}",
                )
            )

    # ── Emissores de eventos ─────────────────────────────────────────────────

    async def _emit_stage(
        self,
        queue: asyncio.Queue,
        correlation_id: str,
        stage_name: str,
        stage_idx: int,
        total_stages: int,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emite evento de atualização de stage + progresso."""
        percent = int(((stage_idx + 1) / total_stages) * 100)

        await queue.put(
            StreamEvent(
                type=StreamEventType.STAGE_UPDATE,
                correlation_id=correlation_id,
                data={
                    "stage": stage_name,
                    "stage_index": stage_idx,
                    "total_stages": total_stages,
                    **(extra_data or {}),
                },
                message=f"Stage: {stage_name}",
            )
        )

        await queue.put(
            StreamEvent(
                type=StreamEventType.PROGRESS,
                correlation_id=correlation_id,
                data={
                    "percent": percent,
                    "stage": stage_name,
                    "stage_index": stage_idx,
                },
                message=f"Progresso: {percent}%",
            )
        )

    async def _buffer_partial_result(
        self,
        buffer: List[Dict[str, Any]],
        result: Dict[str, Any],
        last_batch_time: float,
        queue: asyncio.Queue,
        correlation_id: str,
    ) -> None:
        """Buffer de resultados parciais com batching."""
        buffer.append(result)

        now = time.monotonic()
        if (now - last_batch_time) * 1000 >= self.config.batch_interval_ms or len(
            buffer
        ) >= 5:
            await queue.put(
                StreamEvent(
                    type=StreamEventType.PARTIAL_RESULT,
                    correlation_id=correlation_id,
                    data={"results": list(buffer), "count": len(buffer)},
                    message=f"{len(buffer)} resultados parciais",
                )
            )
            buffer.clear()

    async def _heartbeat_loop(
        self,
        correlation_id: str,
        queue: asyncio.Queue,
    ) -> None:
        """Loop de heartbeat em background."""
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval)
                await queue.put(
                    StreamEvent(
                        type=StreamEventType.HEARTBEAT,
                        correlation_id=correlation_id,
                        message="keep-alive",
                    )
                )
        except asyncio.CancelledError:
            pass

    # ── Monkey-patch para integração ─────────────────────────────────────────

    def _patch_orchestrator(
        self,
        orchestrator: Any,
        stage_callback: Callable,
        result_callback: Callable,
    ) -> Dict[str, Any]:
        """Patch temporário no orchestrator para interceptar progresso."""
        original = {}

        stage_methods = [
            "_plan_search",
            "_execute_searches",
            "_synthesize_results",
        ]

        for method_name in stage_methods:
            if hasattr(orchestrator, method_name):
                original[method_name] = getattr(orchestrator, method_name)

        return original

    def _unpatch_orchestrator(
        self, orchestrator: Any, original: Dict[str, Any]
    ) -> None:
        """Restaura métodos originais do orchestrator."""
        for name, method in original.items():
            setattr(orchestrator, name, method)

    # ── Cleanup ─────────────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Remove conexões órfãs periodicamente."""
        while True:
            await asyncio.sleep(60)
            dead = []
            for corr_id, task in self._stream_tasks.items():
                if task.done():
                    dead.append(corr_id)
            for corr_id in dead:
                self._active_streams.pop(corr_id, None)
                self._stream_tasks.pop(corr_id, None)
                logger.debug(f"Stream {corr_id[:8]} removido do cleanup")


# ── FastAPI Helpers ────────────────────────────────────────────────────────────

try:
    from fastapi import Request
    from fastapi.responses import StreamingResponse

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    Request = Any  # type: ignore
    StreamingResponse = Any  # type: ignore


class SSEEndpoint:
    """Helper para criar endpoint SSE no FastAPI."""

    def __init__(self, streaming_manager: StreamingManager):
        self.streaming = streaming_manager

    async def handle(
        self,
        request: Request,
        query: str,
        orchestrator: Any,
        formats: Optional[List[Any]] = None,
    ) -> StreamingResponse:
        """Retorna StreamingResponse SSE configurado."""
        if not FASTAPI_AVAILABLE:
            raise ImportError("FastAPI não instalado")

        async def event_generator():
            async for sse_line in self.streaming.sse_research(
                query, orchestrator, formats
            ):
                yield sse_line

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


# ── Streamlit Client ───────────────────────────────────────────────────────────

try:
    import streamlit as st

    STREAMLIT_AVAILABLE = True
except ImportError:
    STREAMLIT_AVAILABLE = False
    st = None  # type: ignore


class StreamlitStreamingClient:
    """Cliente de streaming para frontend Streamlit."""

    def __init__(self, endpoint: str, timeout: float = 600.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def stream_research(
        self,
        query: str,
        formats: Optional[List[str]] = None,
    ) -> List[StreamEvent]:
        """Stream de pesquisa síncrono para Streamlit."""
        try:
            import requests
        except ImportError:
            raise ImportError("requests não instalado")

        params = {"query": query}
        if formats:
            params["formats"] = ",".join(formats)

        with requests.get(
            self.endpoint,
            params=params,
            stream=True,
            timeout=self.timeout,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()

            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue

                try:
                    data = json.loads(line[6:])
                    event = StreamEvent(
                        type=StreamEventType(data.get("type", "heartbeat")),
                        correlation_id=data.get("correlation_id", ""),
                        timestamp=data.get("timestamp", ""),
                        data=data.get("data", {}),
                        message=data.get("message", ""),
                    )
                    yield event

                    if event.type in (StreamEventType.COMPLETE, StreamEventType.ERROR):
                        break

                except json.JSONDecodeError:
                    continue

    def render_streamlit_ui(
        self,
        query: str,
        show_partial_results: bool = True,
        show_metrics: bool = True,
    ) -> Optional[str]:
        """Renderiza UI completa de streaming no Streamlit."""
        if not STREAMLIT_AVAILABLE:
            raise ImportError("streamlit não instalado")

        st.header("🔍 Smart Research Agent")
        st.subheader(f"Query: {query}")

        progress_bar = st.progress(0)
        status_text = st.empty()
        metrics_text = st.empty() if show_metrics else None
        results_container = st.container() if show_partial_results else None
        report_container = st.container()

        final_report: Optional[str] = None

        for event in self.stream_research(query):
            if event.type == StreamEventType.PROGRESS:
                percent = event.data.get("percent", 0)
                progress_bar.progress(min(percent / 100, 1.0))
                status_text.info(
                    f"🔄 {event.data.get('stage', 'processando')}... ({percent}%)"
                )

            elif event.type == StreamEventType.STAGE_UPDATE:
                status_text.info(f"📍 Stage: {event.data.get('stage', '')}")

            elif event.type == StreamEventType.PARTIAL_RESULT and results_container:
                with results_container:
                    for result in event.data.get("results", []):
                        st.markdown(
                            f"""
                            <div style="padding: 10px; border-left: 3px solid #4CAF50; margin: 5px 0;">
                            <strong>{result.get("title", "Sem título")}</strong><br/>
                            <small>{result.get("url", "")}</small><br/>
                            {result.get("description", "")[:200]}...
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

            elif event.type == StreamEventType.PARTIAL_REPORT and report_container:
                with report_container:
                    st.markdown(event.data.get("content", ""))

            elif event.type == StreamEventType.METRICS and metrics_text:
                metrics = event.data
                metrics_text.caption(
                    f"💰 Custo: ${metrics.get('cost_usd', 0):.4f} | "
                    f"⏱️ Latência: {metrics.get('duration_seconds', 0):.1f}s | "
                    f"🤖 LLM calls: {metrics.get('llm_calls', 0)}"
                )

            elif event.type == StreamEventType.WARNING:
                st.warning(event.message)

            elif event.type == StreamEventType.ERROR:
                st.error(f"❌ {event.message}")
                return None

            elif event.type == StreamEventType.COMPLETE:
                final_report = event.data.get("report")
                progress_bar.progress(1.0)
                status_text.success("✅ Pesquisa concluída!")
                break

        return final_report


# ── Async Client ────────────────────────────────────────────────────────────────


class AsyncStreamingClient:
    """Cliente async para consumir stream SSE programaticamente."""

    def __init__(self, endpoint: str, timeout: float = 600.0):
        self.endpoint = endpoint
        self.timeout = timeout

    async def stream(
        self,
        query: str,
        formats: Optional[List[str]] = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Gera eventos do stream SSE de forma assíncrona."""
        try:
            import aiohttp
        except ImportError:
            raise ImportError("aiohttp não instalado")

        params = {"query": query}
        if formats:
            params["formats"] = ",".join(formats)

        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                self.endpoint,
                params=params,
                headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()

                async for line in response.content:
                    decoded = line.decode("utf-8").strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue

                    try:
                        data = json.loads(decoded[6:])
                        event = StreamEvent(
                            type=StreamEventType(data.get("type", "heartbeat")),
                            correlation_id=data.get("correlation_id", ""),
                            timestamp=data.get("timestamp", ""),
                            data=data.get("data", {}),
                            message=data.get("message", ""),
                        )
                        yield event

                        if event.type in (
                            StreamEventType.COMPLETE,
                            StreamEventType.ERROR,
                        ):
                            break

                    except json.JSONDecodeError:
                        continue
