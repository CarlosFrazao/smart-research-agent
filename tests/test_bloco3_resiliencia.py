"""
Tests para Bloco 3: Circuit Breaker, Retry Decorator, SmartCache
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from src.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
)
from src.utils.retry import with_retry, RetryConfig
from src.cache import Cache as SmartCache


# ─────────────────────────── CIRCUIT BREAKER TESTS ───────────────────────────


class TestCircuitBreaker:
    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Limpa o registry entre testes para evitar estado compartilhado."""
        CircuitBreakerRegistry.reset_all()
        yield
        CircuitBreakerRegistry.reset_all()

    @pytest.mark.asyncio
    async def test_circuit_starts_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_success_keeps_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        ok_func = AsyncMock(return_value="ok")

        result = await cb.call(ok_func)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_three_failures_open_circuit(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=600)
        fail_func = AsyncMock(side_effect=RuntimeError("api error"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail_func)

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    @pytest.mark.asyncio
    async def test_open_circuit_raises_circuit_breaker_open(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=600)
        fail_func = AsyncMock(side_effect=RuntimeError("api error"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail_func)

        # Agora deve levantar CircuitBreakerOpen sem chamar a função
        with pytest.raises(CircuitBreakerOpen):
            await cb.call(fail_func)
        assert fail_func.call_count == 3  # não chamou 4ª vez

    @pytest.mark.asyncio
    async def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=0.01)
        fail_func = AsyncMock(side_effect=RuntimeError("api error"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail_func)

        assert cb.state == CircuitState.OPEN
        await asyncio.sleep(0.05)  # aguarda recovery_timeout

        # Próxima chamada deve transitar para HALF_OPEN antes de executar
        ok_func = AsyncMock(return_value="recovered")
        result = await cb.call(ok_func)
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED  # HALF_OPEN → CLOSED após sucesso

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=0.01)
        fail_func = AsyncMock(side_effect=RuntimeError("api error"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail_func)

        await asyncio.sleep(0.05)

        # Falha no HALF_OPEN deve reabrir
        with pytest.raises(RuntimeError):
            await cb.call(fail_func)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_registry_reuses_same_breaker(self):
        cb1 = CircuitBreakerRegistry.get("github_api")
        cb2 = CircuitBreakerRegistry.get("github_api")
        assert cb1 is cb2

    @pytest.mark.asyncio
    async def test_registry_status_all(self):
        CircuitBreakerRegistry.get("svc_a")
        CircuitBreakerRegistry.get("svc_b")
        status = CircuitBreakerRegistry.status_all()
        assert "svc_a" in status
        assert "svc_b" in status
        assert status["svc_a"] == "closed"


# ─────────────────────────── RETRY DECORATOR TESTS ───────────────────────────


class TestRetryDecorator:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        call_count = 0

        @with_retry(RetryConfig(max_retries=3, base_delay=0.01))
        async def my_func():
            nonlocal call_count
            call_count += 1
            return "done"

        result = await my_func()
        assert result == "done"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self):
        attempts = []

        @with_retry(RetryConfig(max_retries=3, base_delay=0.01, jitter=False))
        async def unstable():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("temporary error")
            return "ok"

        with patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await unstable()

        assert result == "ok"
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        call_count = 0

        @with_retry(RetryConfig(max_retries=2, base_delay=0.01, jitter=False))
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("permanent error")

        with patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="permanent error"):
                await always_fails()

        assert call_count == 3  # 1 attempt + 2 retries

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self):
        sleep_calls = []

        @with_retry(
            RetryConfig(
                max_retries=3, base_delay=2.0, exponential_base=2.0, jitter=False
            )
        )
        async def always_fails():
            raise RuntimeError("fail")

        with patch(
            "src.utils.retry.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_sleep.side_effect = lambda d: sleep_calls.append(d)
            with pytest.raises(RuntimeError):
                await always_fails()

        assert len(sleep_calls) == 3
        assert abs(sleep_calls[0] - 2.0) < 0.01  # 2^0 * 2.0
        assert abs(sleep_calls[1] - 4.0) < 0.01  # 2^1 * 2.0
        assert abs(sleep_calls[2] - 8.0) < 0.01  # 2^2 * 2.0


# ─────────────────────────── SMART CACHE TESTS ───────────────────────────────


class TestSmartCache:
    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self):
        cache = SmartCache()
        result = await cache.get("key_not_exists")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get(self):
        cache = SmartCache()
        await cache.set("my_key", {"data": [1, 2, 3]}, ttl_seconds=60)
        result = await cache.get("my_key")
        assert result == {"data": [1, 2, 3]}

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        cache = SmartCache()
        await cache.set("short_key", "value", ttl_seconds=0)  # TTL de 0s já expirado
        # Forçar vencimento setando expires no passado
        from datetime import datetime, timezone, timedelta

        data = cache.memory.get("short_key")
        if data:
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            data["expires"] = past
            cache.memory["short_key"] = data
        result = await cache.get("short_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self):
        cache = SmartCache()
        await cache.set("del_key", "to_delete", ttl_seconds=60)
        assert await cache.get("del_key") == "to_delete"
        await cache.delete("del_key")
        assert await cache.get("del_key") is None

    @pytest.mark.asyncio
    async def test_make_key_is_deterministic(self):
        cache = SmartCache()
        k1 = cache.make_key("github", "open source CRM")
        k2 = cache.make_key("github", "open source CRM")
        k3 = cache.make_key("github", "different query")
        assert k1 == k2
        assert k1 != k3

    @pytest.mark.asyncio
    async def test_ttl_strategies_applied(self):
        cache = SmartCache()
        # github deve ter TTL de 3600 (1h), news 900
        await cache.set("g1", "val", source_type="github")
        await cache.set("n1", "val2", source_type="news")
        from datetime import datetime, timezone

        g_expires = datetime.fromisoformat(cache.memory["g1"]["expires"])
        n_expires = datetime.fromisoformat(cache.memory["n1"]["expires"])
        now = datetime.now(timezone.utc)
        assert (g_expires - now).seconds > 3500
        assert (n_expires - now).seconds > 880 and (n_expires - now).seconds < 910
