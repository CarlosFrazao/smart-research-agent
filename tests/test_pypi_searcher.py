import pytest
from unittest.mock import AsyncMock
from src.search.pypi_searcher import PyPISearcher

@pytest.mark.asyncio
async def test_pypi_searcher_init():
    """Testa inicialização do PyPISearcher."""
    config = {"timeout": 10}
    searcher = PyPISearcher(config)
    assert searcher.source_name == "pypi"
    assert searcher.base_url == "https://pypi.org"

@pytest.mark.asyncio
async def test_pypi_create_from_item():
    """Testa criação de SearchResult a partir de item PyPI."""
    config = {}
    searcher = PyPISearcher(config)

    item = {
        "name": "requests",
        "summary": "Python HTTP library",
        "version": "2.31.0",
        "author": "Kenneth Reitz",
        "license": "Apache-2.0"
    }

    result = searcher._create_from_pypi_item(item)
    assert result is not None
    assert result.source == "pypi"
    assert result.title == "requests"
    assert result.url == "https://pypi.org/project/requests/"
    assert result.description == "Python HTTP library"
    assert result.metrics["version"] == "2.31.0"
    assert result.metrics["author"] == "Kenneth Reitz"

@pytest.mark.asyncio
async def test_pypi_search_success():
    """Testa busca bem-sucedida com mock."""
    mock_response = {
        "name": "requests",
        "summary": "Python HTTP for Humans",
        "version": "2.31.0"
    }

    config = {}
    searcher = PyPISearcher(config)
    searcher._make_request = AsyncMock(return_value=mock_response)

    results = await searcher.search("requests")
    assert len(results) == 1
    assert results[0].title == "requests"
    assert results[0].source == "pypi"

@pytest.mark.asyncio
async def test_pypi_search_empty_results():
    """Testa busca que retorna vazio."""
    config = {}
    searcher = PyPISearcher(config)
    searcher._make_request = AsyncMock(return_value=None)
    searcher._fetch_pypi_data = AsyncMock(return_value=None)

    results = await searcher.search("nonexistent_package_xyz")
    assert results == []