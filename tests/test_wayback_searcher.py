import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.search.wayback_searcher import WaybackSearcher
from src.types import SearchResult

@pytest.mark.asyncio
async def test_wayback_searcher_non_url_returns_empty():
    config = {}
    searcher = WaybackSearcher(config)
    
    # Wayback deve ignorar se a query não for URL
    results = await searcher.search("python programming")
    assert results == []


@pytest.mark.asyncio
async def test_wayback_searcher_success():
    config = {
        "timeout": 10
    }
    
    searcher = WaybackSearcher(config)
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20150315000000/http://example.com",
                "timestamp": "20150315123000",
                "status": "200"
            }
        }
    })
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        results = await searcher.search("http://example.com")
        
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert "15/03/2015 12:30:00" in results[0].title
        assert results[0].url == "http://web.archive.org/web/20150315000000/http://example.com"
        assert results[0].source == "wayback"
        assert results[0].metrics["timestamp"] == "20150315123000"
        assert results[0].metrics["status"] == "200"
