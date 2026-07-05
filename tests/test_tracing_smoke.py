import pytest
import uuid
from src.monitoring.tracing import (
    TracingManager,
    TracingConfig,
    trace_span,
    trace_block,
    get_correlation_id,
    set_correlation_id,
    clear_correlation_id,
    SpanKind,
)

@pytest.mark.asyncio
async def test_tracing_manager_initialization_and_correlation_id():
    # Zera singleton para garantir isolamento
    TracingManager._instance = None
    TracingManager._initialized = False

    config = TracingConfig(enabled=True, service_name="test-sra")
    manager = TracingManager(config)

    # Inicializa (deve ir para fallback ou OTel)
    manager.init()

    assert manager.config.service_name == "test-sra"

    # Testa Correlation ID
    clear_correlation_id()
    cid = get_correlation_id()
    assert cid is not None
    assert isinstance(uuid.UUID(cid), uuid.UUID)

    set_correlation_id("my-custom-correlation-id")
    assert get_correlation_id() == "my-custom-correlation-id"
    clear_correlation_id()

@pytest.mark.asyncio
async def test_trace_span_decorator():
    # Zera singleton
    TracingManager._instance = None
    TracingManager._initialized = False

    manager = TracingManager(TracingConfig(enabled=True))
    manager.init()

    # Função async decorada
    @trace_span(name="test.span.function", kind=SpanKind.INTERNAL)
    async def dummy_function(x, y):
        return x + y

    result = await dummy_function(5, 10)
    assert result == 15

    # Se OTel não estiver instalado, os spans ficam no fallback em memória
    if not manager.is_available:
        assert len(manager._fallback_spans) > 0
        span = manager._fallback_spans[-1]
        assert span.span_name == "test.span.function"
        assert span.status == "ok"

@pytest.mark.asyncio
async def test_trace_block_context_manager():
    # Zera singleton
    TracingManager._instance = None
    TracingManager._initialized = False

    manager = TracingManager(TracingConfig(enabled=True))
    manager.init()

    # Context manager síncrono/async
    with trace_block("test.block.name", kind=SpanKind.CLIENT, attributes={"env": "test"}):
        a = 2 + 2
        assert a == 4

    if not manager.is_available:
        assert len(manager._fallback_spans) > 0
        span = manager._fallback_spans[-1]
        assert span.span_name == "test.block.name"
        assert span.attributes["env"] == "test"
        assert span.status == "ok"
