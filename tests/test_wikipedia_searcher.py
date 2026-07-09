import pytest
from unittest.mock import AsyncMock, MagicMock
import aiohttp
from src.search.wikipedia_searcher import WikipediaSearcher

@pytest.mark.asyncio
async def test_wikipedia_searcher_init():
    """Testa inicialização correta do WikipediaSearcher."""
    config = {"lang": "pt", "timeout": 10}
    searcher = WikipediaSearcher(config)
    assert searcher.lang == "pt"
    assert searcher._base_url == "https://pt.wikipedia.org"

@pytest.mark.asyncio
async def test_wikipedia_searcher_normalize():
    """Testa normalização de resultado da Wikipedia."""
    config = {}
    searcher = WikipediaSearcher(config)
    raw_result = {
        "title": "Test Article",
        "pageid": 12345,
        "snippet": "Este é um artigo de teste sobre algo interessante.",
        "wordcount": 1200,
    }
    result = searcher.normalize(raw_result)
    assert result.source == "wikipedia"
    assert result.title == "Test Article"
    assert result.url == "https://en.wikipedia.org/wiki/Test_Article"
    assert result.description == "Este é um artigo de teste sobre algo interessante."
    assert result.metrics["pageid"] == 12345
    assert result.metrics["wordcount"] == 1200

@pytest.mark.asyncio
async def test_wikipedia_searcher_search_success():
    """Testa busca bem-sucedida usando mock."""
    mock_response = {
        "query": {
            "search": [
                {
                    "title": "Teste",
                    "pageid": 999,
                    "snippet": "Resultado de teste",
                    "wordcount": 150
                }
            ]
        }
    }

    config = {}
    searcher = WikipediaSearcher(config)
    searcher._make_request = AsyncMock(return_value=mock_response)

    results = await searcher.search("teste")
    assert len(results) == 1
    assert results[0].title == "Teste"
    assert results[0].source == "wikipedia"
    assert results[0].url == "https://en.wikipedia.org/wiki/Teste"