import asyncio
import pytest
from src.utils.retry import retry_with_backoff, retry_call, RetryConfig
from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpen, CircuitState

class DummyException(Exception):
    pass

@pytest.mark.asyncio
async def test_retry_success_on_first_try():
    calls = 0

    @retry_with_backoff(max_attempts=3, min_wait=0.01)
    async def sample_func():
        nonlocal calls
        calls += 1
        return "success"

    result = await sample_func()
    assert result == "success"
    assert calls == 1

@pytest.mark.asyncio
async def test_retry_success_after_failures():
    calls = 0

    @retry_with_backoff(max_attempts=3, min_wait=0.01, expected_exceptions=(DummyException,))
    async def sample_func():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DummyException("fail")
        return "recovered"

    result = await sample_func()
    assert result == "recovered"
    assert calls == 3

@pytest.mark.asyncio
async def test_retry_exhaustion():
    calls = 0

    @retry_with_backoff(max_attempts=3, min_wait=0.01, expected_exceptions=(DummyException,))
    async def sample_func():
        nonlocal calls
        calls += 1
        raise DummyException(f"fail {calls}")

    with pytest.raises(DummyException) as exc_info:
        await sample_func()
    
    assert "fail 3" in str(exc_info.value)
    assert calls == 3

@pytest.mark.asyncio
async def test_retry_circuit_breaker_integration_open():
    # Cria um Circuit Breaker e força ele a OPEN
    config = CircuitBreakerConfig(name="test_cb", failure_threshold=2, recovery_timeout=60.0)
    cb = CircuitBreaker(config)
    cb.record_failure("error 1")
    cb.record_failure("error 2")
    assert cb.state == CircuitState.OPEN

    calls = 0

    @retry_with_backoff(max_attempts=3, min_wait=0.01, circuit_breaker=cb)
    async def sample_func():
        nonlocal calls
        calls += 1
        return "should not be called"

    with pytest.raises(CircuitBreakerOpen):
        await sample_func()
        
    assert calls == 0

@pytest.mark.asyncio
async def test_retry_call_direct_async():
    calls = 0
    
    async def target(x):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise DummyException("fail")
        return x * 2

    val = await retry_call(target, 5, max_attempts=3, min_wait=0.01, expected_exceptions=(DummyException,))
    assert val == 10
    assert calls == 2
