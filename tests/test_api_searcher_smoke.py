import pytest
import httpx
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock
from src.search.api_searcher import APISearcher, APISearcherConfig
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerConfig, CircuitBreakerOpen

class DummyAPISearcher(APISearcher):
    def __init__(self, config: APISearcherConfig):
        super().__init__(config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        params = {"q": query}
        # Chama a API protegida
        data = await self._make_request("GET", "/search", params=params)
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self.normalize(item) for item in items]

    def normalize(self, raw_result: Any) -> SearchResult:
        return SearchResult(
            source=self.source_name,
            title=raw_result.get("name", "Unknown"),
            url=raw_result.get("html_url", "http://example.com"),
            description=raw_result.get("desc", ""),
            metrics={},
        )

@pytest.mark.asyncio
async def test_api_searcher_success():
    cfg = APISearcherConfig(
        source_name="dummy",
        base_url="https://api.dummy.com",
        max_results=5,
    )
    searcher = DummyAPISearcher(cfg)

    # Mock client and call
    mock_client = AsyncMock()
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json.return_value = {
        "items": [
            {"name": "Rust repo", "html_url": "https://github.com/rust", "desc": "Fast language"}
        ]
    }
    mock_client.get.return_value = mock_response

    with patch.object(searcher, "_get_client", return_value=mock_client):
        results = await searcher.search("rust")

    assert len(results) == 1
    assert results[0].title == "Rust repo"
    assert results[0].source == "dummy"
    await searcher.close()

@pytest.mark.asyncio
async def test_api_searcher_circuit_breaker_open():
    # Cria uma configuração com circuit breaker
    cb_config = CircuitBreakerConfig(name="dummy_cb", failure_threshold=2, recovery_timeout=60.0)
    cfg = APISearcherConfig(
        source_name="dummy",
        base_url="https://api.dummy.com",
        circuit_config=cb_config,
    )
    searcher = DummyAPISearcher(cfg)

    # Força falha no breaker
    breaker = await searcher._ensure_circuit()
    breaker.record_failure("error 1")
    breaker.record_failure("error 2")
    assert breaker.state.value == "open"

    with pytest.raises(CircuitBreakerOpen):
        await searcher.search("rust")
        
    await searcher.close()
