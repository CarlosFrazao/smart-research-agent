import pytest
from unittest.mock import AsyncMock
from src.search.duckduckgo_searcher import DuckDuckGoSearcher

@pytest.mark.asyncio
async def test_duckduckgo_searcher_init():
    """Testa inicialização do DuckDuckGoSearcher."""
    config = {"timeout": 15}
    searcher = DuckDuckGoSearcher(config)
    assert searcher.source_name == "duckduckgo"
    assert searcher.base_url == "https://api.duckduckgo.com"

@pytest.mark.asyncio
async def test_duckduckgo_parse_response_with_related_topics():
    """Testa parse de resposta com RelatedTopics."""
    config = {}
    searcher = DuckDuckGoSearcher(config)

    data = {
        "RelatedTopics": [
            {
                "Text": "Resultado 1",
                "FirstURL": "https://example.com/1"
            },
            {
                "Text": "Resultado 2",
                "FirstURL": "https://example.com/2"
            }
        ]
    }

    results = searcher._parse_response(data)
    assert len(results) == 2
    assert results[0].title == "Resultado 1"
    assert results[0].url == "https://example.com/1"
    assert results[0].source == "duckduckgo"

@pytest.mark.asyncio
async def test_duckduckgo_parse_response_empty():
    """Testa parse com resposta vazia."""
    config = {}
    searcher = DuckDuckGoSearcher(config)

    results = searcher._parse_response({})
    assert results == []

@pytest.mark.asyncio
async def test_duckduckgo_search_success():
    """Testa busca bem-sucedida com mock."""
    mock_response = {
        "RelatedTopics": [
            {"Text": "Python programming", "FirstURL": "https://example.com/python"}
        ]
    }

    config = {}
    searcher = DuckDuckGoSearcher(config)
    searcher._make_request = AsyncMock(return_value=mock_response)

    results = await searcher.search("python")
    assert len(results) == 1
    assert results[0].title == "Python programming"
    assert results[0].source == "duckduckgo"