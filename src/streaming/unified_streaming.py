"""Unified Streaming Module — Combines ProgressBroker from legacy streaming with StreamingManager from src/api/streaming.

This module provides unified, robust streaming infrastructure for the Smart Research Agent,
supporting SSE, WebSocket fallback, and Streamlit integration while unifying legacy utilities.

Key Features:
- Unified ProgressEvent class compatible with both SSE and WebSocket protocols
- Seamless integration with FastAPI, Streamlit, and traditional progress callbacks
- Heartbeat management and timeout handling for all stream types
- Centralized cleanup and resource management for multiple concurrent connections

Migrates from:
1. ProgressBroker, ProgressEvent, format_sse, format_sse_comment (from api/streaming.py)
2. StreamEventType, StreamEvent (from src/api/streaming.py)

Maintains full backward compatibility while providing enhanced reliability and flexibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Literal


logger = logging.getLogger(__name__)

# ── Event Types and Data Models ─────────────────────────────────────────────────

EventType = Literal[
    "progress", "result", "error", "heartbeat", "stage_update", "partial_result"
]


# Core progress event supporting all streaming protocols
@dataclass
class ProgressEvent:
    task_id: str
    event: EventType = "progress"
    step: int = 0
    total_steps: int = 0
    message: str = ""
    percent: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @classmethod
    def progress(
        cls, task_id: str, step: int, total_steps: int, message: str
    ) -> "ProgressEvent":
        """Create a progress event with automatic percentage calculation."""
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
    def result(
        cls, task_id: str, data: Dict[str, Any], total_steps: int = 0
    ) -> "ProgressEvent":
        """Create terminal success event with final payload."""
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
        """Create terminal error event."""
        return cls(
            task_id=task_id,
            event="error",
            message="Falha na tarefa",
            error=error,
        )

    def to_sse(self) -> str:
        """Serialize to SSE format for web clients."""
        payload = asdict(self)
        payload["event"] = self.event
        payload["id"] = f"{self.task_id}-{self.step}-{int(time.time() * 1000)}"
        return f"id: {payload['id']}\nevent: {self.event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_ws(self) -> str:
        """Serialize to WebSocket JSON format."""
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False)


# Streaming event types with enhanced metadata for rich UI responses
@dataclass
class StreamEventType(str, Enum):
    CONNECTED = "connected"
    STAGE_UPDATE = "stage_update"
    PROGRESS = "progress"
    PARTIAL_RESULT = "partial_result"
    PARTIAL_REPORT = "partial_report"
    METRICS = "metrics"
    WARNING = "warning"
    ERROR = "error"
    COMPLETE = "complete"
    HEARTBEAT = "heartbeat"
    DISCONNECT = "disconnect"


@dataclass
class StreamEvent:
    type: StreamEventType
    correlation_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_sse(self) -> str:
        payload = {
            "type": self.type.value,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "data": self.data,
            "message": self.message,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_ws(self) -> str:
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


# ── Core Unified Streaming Manager ─────────────────────────────────────────────────────────────


class UnifiedStreamingManager:
    """Centralized streaming manager unifying all SSE, WebSocket, and callback capabilities.

    This class provides:
    - Unified ProgressEvent handling across all streaming protocols
    - Integration with legacy ProgressBroker patterns
    - Seamless migration from old and new streaming APIs
    - Coordinated management of SSE, WebSocket, and callback streams
    - Centralized heartbeat, timeout, and connection lifecycle management
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        metrics_collector: Optional[Any] = None,
    ):
        self.config = config or self._default_config()
        self.metrics = metrics_collector
        self._active_streams: Dict[str, asyncio.Queue] = {}
        self._stream_tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    def _default_config(self) -> Dict[str, Any]:
        """Provide default configuration for unified streaming."""
        return {
            "retry_ms": 3000,
            "heartbeat_interval": 15.0,
            "max_duration": 600.0,
            "stages": [
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
            ],
            "enable_partial_results": True,
            "enable_partial_report": True,
            "enable_metrics_stream": True,
            "batch_partial_results": True,
            "batch_interval_ms": 500.0,
        }

    # Legacy ProgressBroker Integration
    def make_progress_callback(self, task_id: str) -> Callable[[int, int, str], None]:
        """Create a legacy progress callback compatible with existing orchestrators."""

        def callback(step: int, total_steps: int, message: str) -> None:
            event = ProgressEvent.progress(task_id, step, total_steps, message)
            self.publish_event(task_id, event)

        return callback

    def publish_event(self, task_id: str, event: ProgressEvent) -> None:
        """Publish event to all connected clients using legacy ProgressBroker pattern."""
        if task_id not in self._active_streams:
            self._active_streams[task_id] = asyncio.Queue()

        queue = self._active_streams[task_id]
        asyncio.create_task(self._publish_to_queue(queue, event))

    async def _publish_to_queue(
        self, queue: asyncio.Queue, event: ProgressEvent
    ) -> None:
        """Internal method to publish event to a specific queue."""
        try:
            await queue.put(event)
        except Exception as e:
            logger.warning(f"Failed to publish event to queue: {e}")

    async def subscribe_to_stream(
        self,
        task_id: str,
        heartbeat_interval: float = None,
    ) -> AsyncGenerator[ProgressEvent, None]:
        """Subscribe to a task's stream with full protocol support."""
        heartbeat_interval = heartbeat_interval or self.config["heartbeat_interval"]
        queue = self._ensure_stream_queue(task_id)

        # Stream existing history for late joiners
        async for event in self._stream_history(task_id):
            yield event

        # Live subscription with heartbeat support
        async for event in self._live_stream(task_id, queue, heartbeat_interval):
            yield event

    async def _ensure_stream_queue(self, task_id: str) -> asyncio.Queue:
        """Ensure a stream queue exists for the given task ID."""
        if task_id not in self._active_streams:
            self._active_streams[task_id] = asyncio.Queue()
        return self._active_streams[task_id]

    async def _stream_history(
        self, task_id: str
    ) -> AsyncGenerator[ProgressEvent, None]:
        """Stream historical events for new subscribers."""
        # For now, this yields nothing since we don't store history in this implementation
        return
        yield

    async def _live_stream(
        self,
        task_id: str,
        queue: asyncio.Queue,
        heartbeat_interval: float,
    ) -> AsyncGenerator[ProgressEvent, None]:
        """Provide live streaming with heartbeat support."""
        try:
            while True:
                timeout = heartbeat_interval
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                    yield event
                    if event.event in ("complete", "error"):
                        break
                except asyncio.TimeoutError:
                    # Heartbeat would be yielded by a separate task
                    continue
        except asyncio.CancelledError:
            pass

    # Enhanced SSE Support with Legacy Compatibility
    async def sse_research_stream(
        self,
        query: str,
        orchestrator: Any,
        formats: Optional[List[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Generate SSE-compatible strings for research pipeline streaming."""
        correlation_id = str(uuid.uuid4())
        task_id = correlation_id  # Use correlation_id as task_id for unified handling

        # Initialize stream and start background processing
        await self._ensure_stream_queue(task_id)
        research_task = asyncio.create_task(
            self._execute_research_pipeline(
                correlation_id=correlation_id,
                query=query,
                orchestrator=orchestrator,
                task_id=task_id,
                formats=formats,
                metadata=metadata,
            ),
            name=f"research:{correlation_id[:8]}",
        )
        self._stream_tasks[correlation_id] = research_task

        # Send connection confirmation
        yield ProgressEvent(
            task_id=task_id,
            event="connected",
            data={
                "query": query,
                "retry": self.config["retry_ms"],
                "stages": self.config["stages"],
            },
            message="Conexão SSE estabelecida",
        ).to_sse()

        # Start heartbeat monitoring
        # capture heartbeat_interval from config so it's visible in _stream_heartbeat
        heartbeat_interval = self.config["heartbeat_interval"]
        heartbeat_task = asyncio.create_task(self._stream_heartbeat(correlation_id))
        start_time = time.monotonic()

        try:
            while True:
                queue = self._active_streams.get(task_id)
                if not queue:
                    break

                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval
                    )
                    yield event.to_sse()

                    if event.event in ("complete", "error"):
                        break

                except asyncio.TimeoutError:
                    if time.monotonic() - start_time > self.config["max_duration"]:
                        yield ProgressEvent(
                            task_id=task_id,
                            event="error",
                            data={"reason": "timeout"},
                            message="Tempo máximo de stream excedido",
                        ).to_sse()
                        break
                    continue

        except asyncio.CancelledError:
            yield ProgressEvent(
                task_id=task_id,
                event="disconnect",
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

            self._active_streams.pop(task_id, None)
            self._stream_tasks.pop(correlation_id, None)

    async def _execute_research_pipeline(
        self,
        correlation_id: str,
        query: str,
        orchestrator: Any,
        task_id: str,
        formats: Optional[List[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Execute the research pipeline with unified event handling."""
        stages = self.config["stages"]
        total_stages = len(stages)
        partial_buffer: List[Dict[str, Any]] = []
        last_batch_time = time.monotonic()

        try:
            # Initialize progress
            await self._emit_progress(
                task_id,
                stage_name=stages[0],
                stage_index=0,
                total_stages=total_stages,
                percent=0,
            )

            # Execute research pipeline
            report = await orchestrator.research(query, formats=formats)

            # Emit completion event
            await self._emit_event(
                task_id,
                "result",
                data={
                    "report_length": len(report),
                    "stages_completed": total_stages,
                    "report": report,
                },
                message="Pesquisa concluída com sucesso",
                percent=100.0,
                step=total_stages,
                total_steps=total_stages,
            )

        except Exception as e:
            logger.error(f"Erro no pipeline de streaming {correlation_id}: {e}")
            await self._emit_event(
                task_id,
                "error",
                data={"error_type": type(e).__name__, "error": str(e)},
                message=f"Erro durante pesquisa: {e}",
            )

    async def _emit_progress(
        self,
        task_id: str,
        stage_name: str,
        stage_index: int,
        total_stages: int,
        percent: int,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit multiple progress events for a single pipeline stage."""
        queue = self._active_streams.get(task_id)
        if not queue:
            return

        # Stage update event
        await queue.put(
            ProgressEvent(
                task_id=task_id,
                event="stage_update",
                step=stage_index,
                total_steps=total_stages,
                message=f"Stage: {stage_name}",
                data={
                    "stage": stage_name,
                    "stage_index": stage_index,
                    "total_stages": total_stages,
                    **(extra_data or {}),
                },
            )
        )

        # Progress event
        await queue.put(
            ProgressEvent(
                task_id=task_id,
                event="progress",
                step=stage_index + 1,
                total_steps=total_stages,
                message=f"Progresso: {percent}%",
                data={
                    "percent": percent,
                    "stage": stage_name,
                    "stage_index": stage_index,
                },
            )
        )

    async def _emit_event(
        self,
        task_id: str,
        event_type: EventType,
        **kwargs,
    ) -> None:
        """Generic event emitter with common parameters."""
        queue = self._active_streams.get(task_id)
        if not queue:
            return

        # Build event from kwargs
        event_data = {k: v for k, v in kwargs.items() if k not in ("task_id", "event")}

        event = ProgressEvent(
            task_id=task_id,
            event=event_type,
            **event_data,
        )
        await queue.put(event)

    async def _stream_heartbeat(self, correlation_id: str) -> None:
        """Background heartbeat task for stream maintenance."""
        try:
            while True:
                await asyncio.sleep(self.config["heartbeat_interval"])
                # Send heartbeat to all active streams
                for task_id in list(self._active_streams.keys()):
                    if (
                        task_id.startswith(correlation_id[:8])
                        or task_id == correlation_id
                    ):
                        await self._emit_event(
                            task_id, "heartbeat", message="keep-alive"
                        )
        except asyncio.CancelledError:
            pass

    # Utility Methods
    async def start(self) -> None:
        """Start background cleanup and management tasks."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("UnifiedStreamingManager: cleanup loop iniciado")

    async def stop(self) -> None:
        """Stop all streaming operations and cleanup resources."""
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
        logger.info("UnifiedStreamingManager: todas as streams paradas")

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of orphaned streams and completed tasks."""
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

    def register_progress_callback(
        self, task_id: str
    ) -> Callable[[int, int, str], None]:
        """Register a progress callback for orchestrator integration."""
        return self.make_progress_callback(task_id)


# Backwards Compatibility Layers

from api.streaming import ProgressBroker  # noqa: E402
from src.api.streaming import StreamingManager  # noqa: E402


class LegacySSEEndpoint:
    """Legacy FastAPI endpoint helper maintaining compatibility with existing code."""

    def __init__(self, streaming_manager: UnifiedStreamingManager):
        self.streaming = streaming_manager

    async def handle(
        self, request: Any, query: str, orchestrator: Any, **kwargs
    ) -> Any:
        """Legacy endpoint handler for backward compatibility."""

        async def event_generator():
            async for sse_line in self.streaming.sse_research_stream(
                query, orchestrator, **kwargs
            ):
                yield sse_line

        return {
            "body": event_generator(),
            "headers": {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
            "status": 200,
        }


def format_sse(event: ProgressEvent) -> str:
    """Serializa um ProgressEvent no formato de wire do SSE."""
    return event.to_sse()


def format_sse_comment(text: str = "keep-alive") -> str:
    """Formata um comentário SSE (linha iniciada com `:`), usado como heartbeat."""
    return f": {text}\n\n"


# Export all public interfaces
__all__ = [
    "ProgressEvent",
    "StreamEventType",
    "StreamEvent",
    "UnifiedStreamingManager",
    "LegacySSEEndpoint",
    "format_sse",
    "format_sse_comment",
    "ProgressBroker",
    "StreamingManager",
]
