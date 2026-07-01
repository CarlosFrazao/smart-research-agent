import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.search.searxng_searcher import SearXNGSearcher
from src.types import SearchResult

@pytest.mark.asyncio
async def test_searxng_searcher_success():
    config = {
        "searxng_url": "http://127.0.0.1:3023",
        "max_results": 5,
        "timeout": 10
    }
    
    searcher = SearXNGSearcher(config)
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "results": [
            {
                "title": "Python Language",
                "url": "https://python.org",
                "content": "Official Python website",
                "score": 4.5,
                "engines": ["google", "bing"]
            }
        ]
    })
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        results = await searcher.search("python")
        
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].title == "Python Language"
        assert results[0].url == "https://python.org"
        assert results[0].source == "searxng"
        assert results[0].metrics["source_domain"] == "python.org"
        assert results[0].metrics["searxng_score"] == 4.5
        assert "google" in results[0].metrics["engines"]


@pytest.mark.asyncio
async def test_searxng_searcher_http_error_falls_back():
    config = {
        "searxng_url": "http://127.0.0.1:3023"
    }
    searcher = SearXNGSearcher(config)
    
    mock_response = MagicMock()
    mock_response.status_code = 500
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        results = await searcher.search("python")
        assert results == []
