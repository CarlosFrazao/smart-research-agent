from unittest.mock import AsyncMock

import pytest

from src.search.npm_searcher import NPMSearcher
from src.search.registry import get_registry
from src.types import SearchResult


def test_npm_searcher_init():
    """Inicialização aponta para o registro do npm."""
    searcher = NPMSearcher({"timeout": 10})
    assert searcher.source_name == "npm"
    assert searcher.base_url == "https://registry.npmjs.org"


@pytest.mark.asyncio
async def test_npm_searcher_search_success():
    """Parse de objects[].package retorna SearchResult corretos."""
    mock_data = {
        "objects": [
            {
                "package": {
                    "name": "express",
                    "description": "Fast, unopinionated, minimalist web framework",
                    "links": {"npm": "https://www.npmjs.com/package/express"},
                    "version": "4.19.2",
                    "keywords": ["web", "framework", "http"],
                }
            }
        ]
    }
    searcher = NPMSearcher({})
    searcher._make_request = AsyncMock(return_value=mock_data)

    results = await searcher.search("express")
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "npm"
    assert results[0].title == "express"
    assert results[0].url == "https://www.npmjs.com/package/express"
    assert "minimalist web framework" in results[0].description
    assert results[0].metrics["version"] == "4.19.2"
    assert results[0].metrics["keywords"] == ["web", "framework", "http"]


@pytest.mark.asyncio
async def test_npm_searcher_search_empty():
    """Resposta vazia ou erro retorna lista vazia sem exceção."""
    searcher = NPMSearcher({})
    searcher._make_request = AsyncMock(return_value={"objects": []})
    assert await searcher.search("nonexistent_pkg_xyz") == []

    searcher2 = NPMSearcher({})
    searcher2._make_request = AsyncMock(side_effect=Exception("boom"))
    assert await searcher2.search("fail") == []


def test_npm_searcher_registered():
    """O searcher está registrado via @register_searcher."""
    assert "npm" in get_registry()
    assert get_registry()["npm"]["cls"] is NPMSearcher
