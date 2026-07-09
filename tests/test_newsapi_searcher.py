import pytest
from unittest.mock import patch, MagicMock
from src.search.newsapi_searcher import NewsAPISearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_newsapi_search_success():
    # Test successful API call
    mock_data = {
        "articles": [
            {
                "title": "Test Article 1",
                "url": "https://example.com/article1",
                "description": "First article description",
                "source": {"name": "Test Source"},
                "publishedAt": "2024-01-15T10:30:00Z"
            },
            {
                "title": "Test Article 2",
                "url": "https://example.com/article2",
                "description": "Second article description",
                "source": {"name": "Another Source"},
                "publishedAt": "2024-01-16T14:45:00Z"
            }
        ]
    }

    config = {"timeout": 10, "max_results": 5, "newsapi_key": "mock-key-123"}
    searcher = NewsAPISearcher(config)

    # Patch the _make_request method on the instance
    with patch.object(searcher, "_make_request", return_value=mock_data):
        results = await searcher.search("test query")

    assert len(results) == 2

    # Validate first result
    r1 = results[0]
    assert r1.source == "newsapi"
    assert r1.title == "Test Article 1"
    assert r1.url == "https://example.com/article1"
    assert "First article description" in r1.description
    assert r1.metrics["publisher"] == "Test Source"
    assert r1.metrics["published_at"] == "2024-01-15T10:30:00Z"

    # Validate second result
    r2 = results[1]
    assert r2.source == "newsapi"
    assert r2.title == "Test Article 2"
    assert r2.url == "https://example.com/article2"
    assert "Second article description" in r2.description
    assert r2.metrics["publisher"] == "Another Source"
    assert r2.metrics["published_at"] == "2024-01-16T14:45:00Z"

    await searcher.close()


@pytest.mark.asyncio
async def test_newsapi_graceful_without_key():
    # Test graceful behavior when API key is missing
    config = {"timeout": 10, "max_results": 5}
    searcher = NewsAPISearcher(config)

    # Force API key to be None to simulate missing key
    searcher._api_key = None

    results = await searcher.search("any query")
    assert results == []

    await searcher.close()