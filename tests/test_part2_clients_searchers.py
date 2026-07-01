import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.types import SearchResult


# ─── LLM Client ───────────────────────────────────────────────────────────────

def test_llm_client_import():
    from src.clients.llm_client import LLMClient, LLMProvider
    assert LLMProvider.ANTHROPIC == "anthropic"
    assert LLMProvider.OPENAI == "openai"


def test_llm_client_init_anthropic():
    with patch("anthropic.AsyncAnthropic"):
        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        assert client.model == "claude-test"
        assert client.provider == LLMProvider.ANTHROPIC


def test_llm_client_init_openai():
    with patch("openai.AsyncOpenAI"):
        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.OPENAI, {"api_key": "test", "model": "gpt-4"})
        assert client.model == "gpt-4"


def test_llm_client_init_ollama():
    with patch("openai.AsyncOpenAI"):
        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.OLLAMA, {"base_url": "http://localhost:11434", "model": "llama3"})
        assert client.model == "llama3"


def test_llm_client_invalid_provider():
    from src.clients.llm_client import LLMClient, LLMProvider
    with pytest.raises(ValueError):
        # Usando um valor de enum inválido diretamente
        client = LLMClient.__new__(LLMClient)
        client.provider = "invalid_provider_xyz"
        client.config = {}
        client._client = None
        client.model = ""
        client._init_client()


@pytest.mark.asyncio
async def test_llm_client_generate_anthropic():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="test response")])
        )
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        result = await client.generate("test prompt")
        assert result == "test response"


@pytest.mark.asyncio
async def test_llm_client_generate_structured():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text='{"key": "value"}')])
        )
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        result = await client.generate_structured("test prompt", {"type": "object"})
        assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_llm_client_generate_structured_strips_markdown():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(
                content=[MagicMock(text='```json\n{"key": "value"}\n```')]
            )
        )
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        client = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        result = await client.generate_structured("test prompt", {"type": "object"})
        assert result == {"key": "value"}


# ─── Firecrawl Client ─────────────────────────────────────────────────────────

def test_firecrawl_client_init_no_app():
    from src.clients.firecrawl_client import FirecrawlClient
    # Instanciar com chave inválida deve não lançar exceção (tem fallback)
    client = FirecrawlClient(api_key="invalid-key")
    # Pode não ter .app se falhar na inicialização — mas o objeto deve existir
    assert client is not None


@pytest.mark.asyncio
async def test_firecrawl_client_search_no_app():
    from src.clients.firecrawl_client import FirecrawlClient
    client = FirecrawlClient(api_key="test")
    client.app = None
    results = await client.search("test query")
    assert results == []


@pytest.mark.asyncio
async def test_firecrawl_client_scrape_no_app():
    from src.clients.firecrawl_client import FirecrawlClient
    client = FirecrawlClient(api_key="test")
    client.app = None
    result = await client.scrape("https://example.com")
    assert result.get("success") is False
    assert "falharam" in result.get("error", "")


# ─── Base Searcher ─────────────────────────────────────────────────────────────

def test_base_searcher_fallback():
    from src.search.base_searcher import BaseSearcher
    from src.types import SearchResult

    class DummySearcher(BaseSearcher):
        async def search(self, query, **kwargs):
            return []
        def normalize(self, raw):
            return SearchResult(source="dummy", title="", url="", description="")

    searcher = DummySearcher({"timeout": 10, "max_results": 5})
    assert searcher.fallback("test") == []
    assert searcher.timeout == 10
    assert searcher.max_results == 5


# ─── GitHub Searcher ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_github_searcher_search_success():
    from src.search.github_searcher import GitHubSearcher

    mock_data = {
        "items": [
            {
                "full_name": "twentyhq/twenty",
                "html_url": "https://github.com/twentyhq/twenty",
                "description": "Open source CRM",
                "stargazers_count": 5000,
                "forks_count": 300,
                "language": "TypeScript",
                "pushed_at": "2026-01-01T00:00:00Z",
                "created_at": "2023-01-01T00:00:00Z",
                "license": {"spdx_id": "MIT"},
                "topics": ["crm"],
                "watchers_count": 5000,
                "open_issues_count": 50,
            }
        ]
    }

    searcher = GitHubSearcher({"timeout": 10, "max_results": 10, "github_token": None})
    searcher.http.get = AsyncMock(return_value=mock_data)

    results = await searcher.search("crm open source")
    assert len(results) == 1
    assert results[0].source == "github"
    assert results[0].title == "twentyhq/twenty"
    assert results[0].metrics["stars"] == 5000


@pytest.mark.asyncio
async def test_github_searcher_fallback_on_error():
    from src.search.github_searcher import GitHubSearcher

    searcher = GitHubSearcher({"timeout": 10, "max_results": 10})
    searcher.http.get = AsyncMock(side_effect=Exception("Network error"))

    results = await searcher.search("crm open source")
    assert results == []


def test_github_searcher_normalize():
    from src.search.github_searcher import GitHubSearcher
    searcher = GitHubSearcher({"timeout": 10, "max_results": 10})
    raw = {
        "full_name": "test/repo",
        "html_url": "https://github.com/test/repo",
        "description": "Test repo",
        "stargazers_count": 100,
        "forks_count": 10,
        "language": "Python",
        "pushed_at": "2026-01-01",
        "created_at": "2023-01-01",
        "license": None,
        "topics": [],
        "watchers_count": 100,
        "open_issues_count": 5,
    }
    result = searcher.normalize(raw)
    assert result.source == "github"
    assert result.metrics["stars"] == 100


# ─── Reddit Searcher ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reddit_searcher_search_success():
    from src.search.reddit_searcher import RedditSearcher

    mock_data = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Best open source CRM 2026",
                        "permalink": "/r/selfhosted/comments/abc",
                        "selftext": "Discussion about CRM",
                        "ups": 500,
                        "num_comments": 50,
                        "subreddit": "selfhosted",
                        "created_utc": 1700000000,
                        "author": "testuser",
                        "score": 500,
                    }
                }
            ]
        }
    }

    class AsyncContextManagerMock:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    from unittest.mock import patch, MagicMock

    searcher = RedditSearcher({"timeout": 10, "max_results": 10})
    searcher.http.get = AsyncMock(return_value=mock_data)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_data)

    with patch("aiohttp.ClientSession.get", return_value=AsyncContextManagerMock(mock_resp)):
        results = await searcher.search("crm open source", domain="saas_b2b")

    assert len(results) == 1
    assert results[0].source == "reddit"
    assert results[0].metrics["upvotes"] == 500
    assert results[0].metrics["subreddit_relevance"] == 25  # selfhosted é relevante para saas_b2b


@pytest.mark.asyncio
async def test_reddit_searcher_fallback_on_error():
    from src.search.reddit_searcher import RedditSearcher
    from src.search.searxng_searcher import SearXNGSearcher
    from unittest.mock import patch, AsyncMock
    
    searcher = RedditSearcher({"timeout": 10, "max_results": 10})
    
    # Mocka todas as estratégias internas do RedditSearcher para retornar []
    # (que é o que elas fazem internamente quando falham)
    searcher._search_via_firecrawl = AsyncMock(return_value=[])
    searcher._search_direct_api = AsyncMock(return_value=[])
    searcher._search_pushshift = AsyncMock(return_value=[])
    
    # Mocka também o SearXNGSearcher para retornar []
    with patch.object(SearXNGSearcher, "search", AsyncMock(return_value=[])):
        results = await searcher.search("test")
        
    assert results == []


# ─── HN Searcher ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hn_searcher_search_success():
    from src.search.hn_searcher import HNSearcher

    mock_data = {
        "hits": [
            {
                "title": "Show HN: Twenty – Open Source CRM",
                "url": "https://twenty.com",
                "story_text": "",
                "points": 300,
                "num_comments": 80,
                "author": "hnuser",
                "created_at": "2026-01-15T10:00:00Z",
                "objectID": "12345",
            }
        ]
    }

    searcher = HNSearcher({"timeout": 10, "max_results": 10})
    searcher.http.get = AsyncMock(return_value=mock_data)

    results = await searcher.search("open source crm")
    assert len(results) == 1
    assert results[0].source == "hackernews"
    assert results[0].metrics["points"] == 300


@pytest.mark.asyncio
async def test_hn_searcher_fallback_url():
    from src.search.hn_searcher import HNSearcher

    mock_data = {
        "hits": [
            {
                "title": "Ask HN: Best CRM?",
                "url": None,
                "story_text": "What CRM do you use?",
                "points": 50,
                "num_comments": 20,
                "author": "user",
                "created_at": "2026-01-01",
                "objectID": "99999",
            }
        ]
    }

    searcher = HNSearcher({"timeout": 10, "max_results": 10})
    searcher.http.get = AsyncMock(return_value=mock_data)

    results = await searcher.search("crm")
    assert results[0].url == "https://news.ycombinator.com/item?id=99999"


# ─── ProductHunt Searcher ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producthunt_searcher_no_token():
    from src.search.producthunt_searcher import ProductHuntSearcher
    searcher = ProductHuntSearcher({"timeout": 10, "max_results": 10})
    results = await searcher.search("crm")
    assert results == []


# ─── Web Searcher ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_searcher_uses_firecrawl():
    from src.search.web_searcher import WebSearcher
    searcher = WebSearcher({"timeout": 10, "max_results": 5, "firecrawl_api_key": "test"})
    searcher.firecrawl.search = AsyncMock(return_value=[
        {"title": "Test Result", "url": "https://example.com", "description": "Test"}
    ])
    results = await searcher.search("test query")
    assert len(results) == 1
    assert results[0].source == "web"


@pytest.mark.asyncio
async def test_web_searcher_fallback():
    from src.search.web_searcher import WebSearcher
    searcher = WebSearcher({"timeout": 10, "max_results": 5, "firecrawl_api_key": "test"})
    searcher.firecrawl.search = AsyncMock(side_effect=Exception("Firecrawl down"))
    results = await searcher.search("test query")
    assert results == []


# ─── Firecrawl Searcher ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_firecrawl_searcher_normalize():
    from src.search.firecrawl_searcher import FirecrawlSearcher
    searcher = FirecrawlSearcher({"timeout": 10, "max_results": 5, "firecrawl_api_key": "test"})
    raw = {"title": "Test", "url": "https://test.com", "markdown": "Content here"}
    result = searcher.normalize(raw)
    assert result.source == "firecrawl"
    assert result.title == "Test"
    assert "Content" in result.description
