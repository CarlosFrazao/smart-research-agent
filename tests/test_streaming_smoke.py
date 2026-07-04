import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from src.api.streaming import StreamingManager, StreamEventType, StreamEvent

@pytest.mark.asyncio
async def test_streaming_manager_sse_research():
    # Setup mocks
    mock_orchestrator = AsyncMock()
    mock_orchestrator.research.return_value = "final report content"
    # Mock dos métodos internos que são monkey-patched
    mock_orchestrator._plan_search = AsyncMock()
    mock_orchestrator._execute_searches = AsyncMock()
    mock_orchestrator._synthesize_results = AsyncMock()

    manager = StreamingManager()

    # Consome o gerador SSE
    events = []
    async for sse_line in manager.sse_research("test query", mock_orchestrator):
        events.append(sse_line)

    # Devem ter sido gerados no mínimo os eventos CONNECTED e COMPLETE
    assert len(events) >= 2
    
    # Valida presença das strings SSE no formato "data: {...}\n\n"
    assert events[0].startswith("data: ")
    assert events[-1].startswith("data: ")
    
    assert "connected" in events[0]
    assert "complete" in events[-1]
