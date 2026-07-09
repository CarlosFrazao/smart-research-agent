from unittest.mock import AsyncMock

import pytest

from src.search.quora_searcher import QuoraSearcher
from src.search.registry import get_registry
from src.search.scraping_searcher import ScrapingError
from src.types import SearchResult


def test_quora_searcher_init():
    """Inicialização define source_name e timeout curto (15s)."""
    searcher = QuoraSearcher({"timeout": 15})
    assert searcher.source_name == "quora"
    assert searcher.timeout == 15.0


@pytest.mark.asyncio
async def test_quora_searcher_search_success():
    """Scraping com âncora de pergunta retorna SearchResult válido."""
    html = (
        '<a href="https://www.quora.com/What-is-Python">What is Python?</a>'
        '<div class="q-text">Python is a popular programming language used for AI.</div>'
    )
    searcher = QuoraSearcher({})
    searcher._scrape_url = AsyncMock(return_value={"markdown": html, "url": "x"})

    results = await searcher.search("python")
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "quora"
    assert results[0].title == "What is Python?"
    assert "quora.com/What-is-Python" in results[0].url
    assert "popular programming language" in results[0].description


@pytest.mark.asyncio
async def test_quora_searcher_search_empty_on_scraping_error():
    """Falha na cascata (ScrapingError) degrada para lista vazia."""
    searcher = QuoraSearcher({})
    searcher._scrape_url = AsyncMock(side_effect=ScrapingError("403 bloqueado"))

    results = await searcher.search("python")
    assert results == []


@pytest.mark.asyncio
async def test_quora_searcher_search_empty_on_short_content():
    """Conteúdo insuficiente (anti-bot/CAPTCHA) degrada para lista vazia."""
    searcher = QuoraSearcher({})
    searcher._scrape_url = AsyncMock(return_value={"markdown": "", "url": "x"})

    results = await searcher.search("python")
    assert results == []


@pytest.mark.asyncio
async def test_quora_searcher_search_empty_on_exception():
    """Qualquer exceção no scraping degrada para lista vazia."""
    searcher = QuoraSearcher({})
    searcher._scrape_url = AsyncMock(side_effect=RuntimeError("explosão"))

    results = await searcher.search("python")
    assert results == []


def test_quora_searcher_registered():
    """O searcher está registrado via @register_searcher."""
    assert "quora" in get_registry()
    assert get_registry()["quora"]["cls"] is QuoraSearcher
