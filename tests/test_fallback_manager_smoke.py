import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from src.pipeline.fallback_manager import (
    FallbackManager,
    FallbackStrategy,
    FallbackExhaustedError,
)
from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry

@pytest.mark.asyncio
async def test_fallback_manager_priority_success():
    manager = FallbackManager()
    await manager.init()

    primary_mock = AsyncMock(return_value="primary_val")
    fallback_mock = AsyncMock(return_value="fallback_val")

    # Caso 1: Primary tem sucesso (fallback não é chamado)
    manager.register(
        stage="search",
        name="test_service",
        primary=primary_mock,
        fallbacks=[("fb_1", fallback_mock)],
        strategy=FallbackStrategy.PRIORITY,
    )

    result = await manager.execute("search", "test_service")
    assert result == "primary_val"
    primary_mock.assert_called_once()
    fallback_mock.assert_not_called()

    # Caso 2: Primary falha (fallback é chamado com sucesso)
    primary_mock.reset_mock()
    primary_mock.side_effect = Exception("Primary failed")

    result_fb = await manager.execute("search", "test_service")
    assert result_fb == "fallback_val"
    primary_mock.assert_called_once()
    fallback_mock.assert_called_once()

@pytest.mark.asyncio
async def test_fallback_manager_exhausted():
    manager = FallbackManager()
    await manager.init()

    primary_mock = AsyncMock(side_effect=Exception("P failed"))
    fallback_mock = AsyncMock(side_effect=Exception("F failed"))

    manager.register(
        stage="scraper",
        name="test_scrape",
        primary=primary_mock,
        fallbacks=[("fb_1", fallback_mock)],
        strategy=FallbackStrategy.PRIORITY,
    )

    with pytest.raises(FallbackExhaustedError) as exc_info:
        await manager.execute("scraper", "test_scrape")

    assert exc_info.value.attempts == 2
    assert "scraper:test_scrape" in str(exc_info.value)

@pytest.mark.asyncio
async def test_fallback_manager_round_robin():
    manager = FallbackManager()
    await manager.init()

    primary_mock = AsyncMock(side_effect=Exception("P failed"))
    fb1_mock = AsyncMock(return_value="fb1")
    fb2_mock = AsyncMock(return_value="fb2")

    manager.register(
        stage="llm",
        name="completion",
        primary=primary_mock,
        fallbacks=[("fb_1", fb1_mock), ("fb_2", fb2_mock)],
        strategy=FallbackStrategy.ROUND_ROBIN,
    )

    # Primeira execução: chama fb_1
    res1 = await manager.execute("llm", "completion")
    assert res1 == "fb1"
    fb1_mock.assert_called_once()
    fb2_mock.assert_not_called()

    # Segunda execução: chama fb_2
    fb1_mock.reset_mock()
    fb2_mock.reset_mock()
    primary_mock.side_effect = Exception("P failed")
    fb1_mock.return_value = "fb1"
    fb2_mock.return_value = "fb2"

    res2 = await manager.execute("llm", "completion")
    assert res2 == "fb2"
    fb1_mock.assert_not_called()
    fb2_mock.assert_called_once()

@pytest.mark.asyncio
async def test_fallback_manager_metrics():
    manager = FallbackManager()
    await manager.init()

    primary_mock = AsyncMock(return_value="ok")
    manager.register(
        stage="cache",
        name="test_cache",
        primary=primary_mock,
        strategy=FallbackStrategy.PRIORITY,
    )

    await manager.execute("cache", "test_cache")
    metrics = manager.get_all_metrics_summary()

    assert metrics["total_registrations"] == 1
    assert metrics["total_invocations"] == 1
    assert metrics["total_success"] == 1
    assert metrics["by_stage"]["cache"]["invocations"] == 1
