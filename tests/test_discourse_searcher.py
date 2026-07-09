import os
from unittest.mock import AsyncMock

import pytest

from src.search.discourse_searcher import DiscourseSearcher
from src.search.registry import get_registry
from src.types import SearchResult


def test_discourse_searcher_init():
    """Inicialização usa o fórum padrão discuss.python.org."""
    searcher = DiscourseSearcher({"timeout": 10})
    assert searcher.source_name == "discourse"
    assert searcher.base_url == "https://discuss.python.org"


def test_discourse_searcher_init_custom_base_url(monkeypatch):
    """URL base configurável via dict ou DISCOURSE_BASE_URL."""
    searcher = DiscourseSearcher({"base_url": "https://meta.discourse.org"})
    assert searcher.base_url == "https://meta.discourse.org"

    monkeypatch.delenv("DISCOURSE_BASE_URL", raising=False)
    searcher2 = DiscourseSearcher({})
    assert searcher2.base_url == "https://discuss.python.org"


@pytest.mark.asyncio
async def test_discourse_searcher_search_success():
    """Parse de posts e tópicos retorna SearchResult corretos."""
    mock_data = {
        "posts": [
            {
                "topic_id": 123,
                "topic_slug": "best-python-tips",
                "post_number": 3,
                "topic_title": "Best Python Tips",
                "blurb": "Use enumerate instead of range(len()).",
                "username": "alice",
            }
        ],
        "topics": [
            {
                "id": 456,
                "slug": "python-async",
                "title": "Python Async Patterns",
                "blurb": "How to structure async code.",
                "posts_count": 12,
                "like_count": 5,
            }
        ],
    }
    searcher = DiscourseSearcher({})
    searcher._make_request = AsyncMock(return_value=mock_data)

    results = await searcher.search("python")
    assert len(results) == 2
    assert all(isinstance(r, SearchResult) for r in results)
    assert results[0].source == "discourse"
    assert results[0].title == "Best Python Tips"
    assert results[0].url == "https://discuss.python.org/t/best-python-tips/123/3"
    assert "enumerate" in results[0].description
    assert results[1].title == "Python Async Patterns"
    assert results[1].url == "https://discuss.python.org/t/python-async/456"


@pytest.mark.asyncio
async def test_discourse_searcher_search_empty():
    """Resposta vazia ou erro retorna lista vazia sem exceção."""
    searcher = DiscourseSearcher({})
    searcher._make_request = AsyncMock(return_value={})
    assert await searcher.search("nothing") == []

    searcher2 = DiscourseSearcher({})
    searcher2._make_request = AsyncMock(side_effect=Exception("boom"))
    assert await searcher2.search("fail") == []


def test_discourse_searcher_registered():
    """O searcher está registrado via @register_searcher."""
    assert "discourse" in get_registry()
    assert get_registry()["discourse"]["cls"] is DiscourseSearcher
