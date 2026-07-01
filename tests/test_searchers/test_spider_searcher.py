import pytest
from unittest.mock import AsyncMock, MagicMock
from src.search.spider_searcher import SpiderSearcher
from src.types import SearchResult


@pytest.fixture
def spider_config():
    return {
        "timeout": 10,
        "max_results": 5,
        "spider_api_key": "test-spider-key",
        "enabled": True,
    }


@pytest.fixture
def spider_searcher(spider_config):
    return SpiderSearcher(spider_config)


# ─── Initialization ──────────────────────────────────────────────────────────

def test_spider_searcher_init(spider_searcher):
    assert spider_searcher.api_key == "test-spider-key"
    assert spider_searcher.max_results == 5
    assert spider_searcher.enabled is True


def test_spider_searcher_init_no_key():
    searcher = SpiderSearcher({"timeout": 10, "max_results": 5})
    assert searcher.api_key == ""


# ─── search() with API response ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spider_searcher_search_list_response(spider_searcher):
    """Spider API returns a list of result objects."""
    mock_response = [
        {
            "url": "https://example.com/page1",
            "content": "This is the scraped markdown content of page one.",
            "metadata": {"title": "Page One"},
            "status": 200,
        }
    ]
    spider_searcher.http.post = AsyncMock(return_value=mock_response)

    results = await spider_searcher.search("https://example.com")

    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "spider.cloud"
    assert results[0].url == "https://example.com/page1"
    assert results[0].title == "Page One"
    assert "scraped markdown" in results[0].description


@pytest.mark.asyncio
async def test_spider_searcher_search_dict_response(spider_searcher):
    """Spider API returns a dict with 'results' key."""
    mock_response = {
        "results": [
            {
                "url": "https://example.com/page2",
                "content": "Content from page two with enough detail.",
                "metadata": {"title": "Page Two"},
                "status": 200,
            }
        ]
    }
    spider_searcher.http.post = AsyncMock(return_value=mock_response)

    results = await spider_searcher.search("https://example.com")

    assert len(results) == 1
    assert results[0].source == "spider.cloud"
    assert results[0].title == "Page Two"


@pytest.mark.asyncio
async def test_spider_searcher_search_empty_response(spider_searcher):
    """Empty response returns empty list without raising."""
    spider_searcher.http.post = AsyncMock(return_value=[])

    results = await spider_searcher.search("https://example.com")

    assert results == []


@pytest.mark.asyncio
async def test_spider_searcher_search_no_api_key():
    """Missing API key triggers fallback (empty list) immediately."""
    searcher = SpiderSearcher({"timeout": 10, "max_results": 5, "spider_api_key": ""})
    searcher.http.post = AsyncMock()

    results = await searcher.search("https://example.com")

    assert results == []
    searcher.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_spider_searcher_fallback_on_error(spider_searcher):
    """Network error activates fallback returning empty list."""
    spider_searcher.http.post = AsyncMock(side_effect=Exception("Connection refused"))

    results = await spider_searcher.search("https://example.com")

    assert results == []


# ─── normalize() ─────────────────────────────────────────────────────────────

def test_spider_searcher_normalize_dict(spider_searcher):
    raw = {
        "url": "https://test.com",
        "content": "Test content here",
        "metadata": {"title": "Test Title"},
        "status": 200,
    }
    result = spider_searcher.normalize(raw)
    assert result.source == "spider.cloud"
    assert result.url == "https://test.com"
    assert result.title == "Test Title"
    assert "Test content" in result.description
    assert result.metrics["status"] == 200


def test_spider_searcher_normalize_string(spider_searcher):
    """Handles plain string content gracefully."""
    result = spider_searcher.normalize("plain markdown content here")
    assert result.source == "spider.cloud"
    assert "plain markdown" in result.description


def test_spider_searcher_normalize_missing_fields(spider_searcher):
    """Handles dict with missing optional fields."""
    raw = {}
    result = spider_searcher.normalize(raw)
    assert result.source == "spider.cloud"
    assert result.url == ""
    assert result.title == ""


# ─── fallback() ──────────────────────────────────────────────────────────────

def test_spider_searcher_fallback_returns_empty(spider_searcher):
    """fallback() always returns empty list."""
    result = spider_searcher.fallback("https://example.com")
    assert result == []


# ─── HTTP call structure ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spider_searcher_uses_correct_endpoint(spider_searcher):
    """Verifies the POST is sent to the correct Spider.cloud endpoint."""
    spider_searcher.http.post = AsyncMock(return_value=[])

    await spider_searcher.search("https://target.com")

    spider_searcher.http.post.assert_called_once()
    call_args = spider_searcher.http.post.call_args
    assert "api.spider.cloud/crawl" in call_args[0][0]


@pytest.mark.asyncio
async def test_spider_searcher_sends_auth_header(spider_searcher):
    """Authorization header contains the API key."""
    spider_searcher.http.post = AsyncMock(return_value=[])

    await spider_searcher.search("https://target.com")

    call_kwargs = spider_searcher.http.post.call_args[1]
    assert "Authorization" in call_kwargs.get("headers", {})
    assert "test-spider-key" in call_kwargs["headers"]["Authorization"]
