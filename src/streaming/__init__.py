"""Unified Streaming Module for Smart Research Agent.

This module provides centralized streaming infrastructure supporting:
- SSE (Server-Sent Events) for web clients
- WebSocket fallback for restricted environments
- Streamlit integration for dashboard updates
- Legacy ProgressBroker pattern compatibility

Main exports:
- UnifiedStreamingManager: Central streaming coordinator
- ProgressEvent: Event dataclass with to_sse()/to_ws() serialization
- StreamEventType: Enum for all event types
- StreamEvent: Enhanced event class for rich streaming
"""

from src.streaming.unified_streaming import (
    UnifiedStreamingManager,
    ProgressEvent,
    StreamEventType,
    StreamEvent,
    LegacySSEEndpoint,
    format_sse,
    format_sse_comment,
)

__all__ = [
    "UnifiedStreamingManager",
    "ProgressEvent",
    "StreamEventType",
    "StreamEvent",
    "LegacySSEEndpoint",
    "format_sse",
    "format_sse_comment",
]
