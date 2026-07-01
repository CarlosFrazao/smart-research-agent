import pytest
from unittest.mock import AsyncMock, MagicMock
from src.search.youtube_searcher import YouTubeSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient


@pytest.mark.asyncio
async def test_youtube_search_success():
    # 1. Configura mock do HTTPClient
    http_mock = MagicMock(spec=HTTPClient)
    
    # Mock do Search API
    search_data = {
        "json": {
            "items": [
                {
                    "id": {"videoId": "vid123"},
                    "snippet": {
                        "title": "Introduction to Quantum Computing",
                        "description": "A basic guide to quantum physics and computing.",
                        "channelTitle": "Physics101",
                        "publishedAt": "2024-05-01T12:00:00Z"
                    }
                }
            ]
        }
    }
    
    # Mock do Videos Statistics API
    videos_data = {
        "json": {
            "items": [
                {
                    "id": "vid123",
                    "statistics": {
                        "viewCount": "100000",
                        "likeCount": "5000"
                    }
                }
            ]
        }
    }
    
    # Configura o mock do HTTPClient para retornar sequencialmente
    http_mock.get = AsyncMock(side_effect=[search_data, videos_data])
    
    # 2. Instancia o YouTubeSearcher com o mock
    config = {"youtube_api_key": "test_key", "timeout": 10, "max_results": 5}
    searcher = YouTubeSearcher(config)
    searcher.http = http_mock
    
    results = await searcher.search("quantum computing")
    
    # 3. Asserções
    assert len(results) == 1
    r = results[0]
    assert r.source == "youtube"
    assert r.title == "Introduction to Quantum Computing"
    assert r.url == "https://www.youtube.com/watch?v=vid123"
    assert "Physics101" in r.description
    assert "100,000" in r.description or "100.000" in r.description or "100" in r.description
    assert r.metrics["views"] == 100000
    assert r.metrics["likes"] == 5000
    assert r.metrics["channel"] == "Physics101"
    assert r.confidence_score > 0.4  # Deve ganhar pontos por views/likes


@pytest.mark.asyncio
async def test_youtube_fallback_triggered_on_no_key():
    # Sem key configurada
    config = {"youtube_api_key": None, "timeout": 10, "max_results": 5}
    
    # Mock do WebSearcher fallback
    web_mock = MagicMock()
    web_mock.enabled = True
    web_mock.search = AsyncMock(return_value=[
        SearchResult(source="web", title="Web YouTube Fallback", url="http://web.com", description="Fallback desc")
    ])
    
    searcher = YouTubeSearcher(config)
    searcher.web_fallback = web_mock
    
    results = await searcher.search("rust tutorial")
    
    assert len(results) == 1
    assert results[0].source == "web"
    assert results[0].title == "Web YouTube Fallback"
    web_mock.search.assert_called_once_with("YouTube video rust tutorial")
