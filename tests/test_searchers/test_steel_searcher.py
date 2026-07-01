import pytest
from unittest.mock import AsyncMock
from src.search.steel_searcher import SteelSearcher
from src.types import SearchResult


@pytest.fixture
def steel_config():
    return {
        "timeout": 10,
        "max_results": 5,
        "steel_api_key": "test-steel-key",
        "enabled": True,
    }


@pytest.fixture
def steel_searcher(steel_config):
    return SteelSearcher(steel_config)


# ─── Initialization ──────────────────────────────────────────────────────────

def test_steel_searcher_init(steel_searcher):
    assert steel_searcher.api_key == "test-steel-key"
    assert steel_searcher.enabled is True


def test_steel_searcher_init_no_key():
    searcher = SteelSearcher({"timeout": 10, "max_results": 5})
    assert searcher.api_key == ""


def test_steel_searcher_custom_base_url():
    searcher = SteelSearcher({
        "timeout": 10,
        "max_results": 5,
        "steel_api_key": "key",
        "steel_base_url": "https://custom.steel.example",
    })
    assert searcher.base_url == "https://custom.steel.example"


# ─── search() ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_steel_searcher_search_success(steel_searcher):
    """Returns a SearchResult when Steel returns content."""
    mock_response = {
        "url": "https://example.com",
        "content": "This is rich JavaScript-rendered content from Steel.dev.",
        "metadata": {"title": "JS Heavy Page"},
        "status": 200,
    }
    steel_searcher.http.post = AsyncMock(return_value=mock_response)

    results = await steel_searcher.search("https://example.com")

    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "steel.dev"
    assert results[0].url == "https://example.com"
    assert results[0].title == "JS Heavy Page"
    assert "JavaScript-rendered" in results[0].description


@pytest.mark.asyncio
async def test_steel_searcher_search_empty_content(steel_searcher):
    """Returns empty list when Steel returns no content."""
    mock_response = {
        "url": "https://example.com",
        "content": "",
        "metadata": {},
        "status": 200,
    }
    steel_searcher.http.post = AsyncMock(return_value=mock_response)

    results = await steel_searcher.search("https://example.com")

    assert results == []


@pytest.mark.asyncio
async def test_steel_searcher_search_no_api_key():
    """Missing API key triggers fallback immediately without HTTP call."""
    searcher = SteelSearcher({"timeout": 10, "max_results": 5, "steel_api_key": ""})
    searcher.http.post = AsyncMock()

    results = await searcher.search("https://example.com")

    assert results == []
    searcher.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_steel_searcher_fallback_on_error(steel_searcher):
    """Network error triggers fallback returning empty list without raising."""
    steel_searcher.http.post = AsyncMock(side_effect=Exception("Steel API down"))

    results = await steel_searcher.search("https://example.com")

    assert results == []


@pytest.mark.asyncio
async def test_steel_searcher_uses_scrape_endpoint(steel_searcher):
    """POST is sent to the /scrape Quick Action endpoint."""
    steel_searcher.http.post = AsyncMock(return_value={"content": "", "url": "", "metadata": {}})

    await steel_searcher.search("https://target.com")

    steel_searcher.http.post.assert_called_once()
    call_url = steel_searcher.http.post.call_args[0][0]
    assert call_url.endswith("/scrape")


@pytest.mark.asyncio
async def test_steel_searcher_sends_proxy_and_captcha(steel_searcher):
    """Payload includes use_proxy=True and solve_captcha=True."""
    steel_searcher.http.post = AsyncMock(return_value={"content": "", "url": "", "metadata": {}})

    await steel_searcher.search("https://target.com")

    call_kwargs = steel_searcher.http.post.call_args[1]
    payload = call_kwargs.get("json", {})
    assert payload.get("use_proxy") is True
    assert payload.get("solve_captcha") is True


@pytest.mark.asyncio
async def test_steel_searcher_sends_auth_header(steel_searcher):
    """Authorization header contains the API key."""
    steel_searcher.http.post = AsyncMock(return_value={"content": "", "url": "", "metadata": {}})

    await steel_searcher.search("https://target.com")

    call_kwargs = steel_searcher.http.post.call_args[1]
    headers = call_kwargs.get("headers", {})
    assert "Authorization" in headers
    assert "test-steel-key" in headers["Authorization"]


# ─── normalize() ─────────────────────────────────────────────────────────────

def test_steel_searcher_normalize_full_response(steel_searcher):
    raw = {
        "url": "https://test.com",
        "content": "Normalized content here",
        "metadata": {"title": "Test Title"},
        "status": 200,
        "solve_captcha": True,
    }
    result = steel_searcher.normalize(raw)
    assert result.source == "steel.dev"
    assert result.url == "https://test.com"
    assert result.title == "Test Title"
    assert "Normalized content" in result.description
    assert result.metrics["status"] == 200


def test_steel_searcher_normalize_markdown_fallback(steel_searcher):
    """Uses 'markdown' key when 'content' is absent."""
    raw = {
        "url": "https://test.com",
        "markdown": "Markdown content here",
        "metadata": {},
        "status": 200,
    }
    result = steel_searcher.normalize(raw)
    assert "Markdown content" in result.description


def test_steel_searcher_normalize_non_dict(steel_searcher):
    """Handles non-dict input gracefully."""
    result = steel_searcher.normalize("unexpected string")
    assert result.source == "steel.dev"
    assert result.description == ""
    assert result.url == ""


def test_steel_searcher_normalize_empty_dict(steel_searcher):
    """Handles empty dict without raising."""
    result = steel_searcher.normalize({})
    assert result.source == "steel.dev"
    assert result.url == ""
    assert result.title == ""


# ─── fallback() ──────────────────────────────────────────────────────────────

def test_steel_searcher_fallback_returns_empty(steel_searcher):
    """fallback() always returns empty list."""
    result = steel_searcher.fallback("https://example.com")
    assert result == []
