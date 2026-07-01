import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.search.stackoverflow_searcher import StackOverflowSearcher
from src.types import SearchResult

@pytest.mark.asyncio
async def test_stackoverflow_searcher_success():
    config = {
        "max_results": 5,
        "timeout": 10
    }
    
    searcher = StackOverflowSearcher(config)
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "items": [
            {
                "title": "How to learn &amp; use Python?",
                "link": "https://stackoverflow.com/questions/1",
                "score": 150,
                "view_count": 25000,
                "is_answered": True,
                "tags": ["python", "programming"],
                "owner": {"display_name": "Carlos"}
            }
        ]
    })
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        results = await searcher.search("python")
        
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        # O título deve vir limpo do HTML entities
        assert results[0].title == "How to learn & use Python?"
        assert results[0].url == "https://stackoverflow.com/questions/1"
        assert results[0].source == "stackoverflow"
        assert results[0].metrics["source_domain"] == "stackoverflow.com"
        assert results[0].metrics["score"] == 150
        assert results[0].metrics["is_answered"] is True
        assert "python" in results[0].metrics["tags"]
        assert "Carlos" in results[0].description
