from unittest.mock import MagicMock

import pytest

from src.search.googletrends_searcher import GoogleTrendsSearcher
from src.search.registry import get_registry
from src.types import SearchResult


def test_googletrends_searcher_init():
    """Inicialização define source_name e disponibilidade conforme ambiente."""
    searcher = GoogleTrendsSearcher({"timeout": 10})
    assert searcher.source_name == "google_trends"
    # available pode ser False se pytrends não estiver instalado — ambos válidos
    assert isinstance(searcher.available, bool)


@pytest.mark.asyncio
async def test_googletrends_searcher_search_success(monkeypatch):
    """Com pytrends disponível e _fetch_trends mockado, retorna 1 resultado."""
    searcher = GoogleTrendsSearcher({})
    searcher.available = True
    searcher.pytrends = object()  # stub não-nulo

    summary = {
        "peak_interest": 92.0,
        "avg_interest": 45.3,
        "last_interest": 60.0,
        "points": 52,
    }
    searcher._fetch_trends = MagicMock(return_value=summary)

    results = await searcher.search("machine learning")
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "google_trends"
    assert results[0].title == "Google Trends: machine learning"
    assert "92%" in results[0].description
    assert "trends.google.com" in results[0].url
    assert results[0].metrics["peak_interest"] == 92.0


@pytest.mark.asyncio
async def test_googletrends_searcher_search_empty_when_unavailable():
    """Sem pytrends, degrada graciosamente para lista vazia."""
    searcher = GoogleTrendsSearcher({})
    searcher.available = False
    searcher.pytrends = None

    results = await searcher.search("any query")
    assert results == []


@pytest.mark.asyncio
async def test_googletrends_searcher_search_empty_on_error(monkeypatch):
    """Erro em _fetch_trends retorna lista vazia sem exceção."""
    searcher = GoogleTrendsSearcher({})
    searcher.available = True
    searcher.pytrends = object()
    searcher._fetch_trends = MagicMock(side_effect=Exception("boom"))

    results = await searcher.search("fail query")
    assert results == []


def test_googletrends_searcher_registered():
    """O searcher está registrado via @register_searcher."""
    assert "google_trends" in get_registry()
    assert get_registry()["google_trends"]["cls"] is GoogleTrendsSearcher
