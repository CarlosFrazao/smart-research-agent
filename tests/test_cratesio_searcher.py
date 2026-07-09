from unittest.mock import AsyncMock

import pytest

from src.search.cratesio_searcher import CratesIOSearcher
from src.search.registry import get_registry
from src.types import SearchResult


def test_cratesio_searcher_init():
    """Inicialização aponta para o crates.io e define User-Agent."""
    searcher = CratesIOSearcher({"timeout": 10})
    assert searcher.source_name == "cratesio"
    assert searcher.base_url == "https://crates.io"
    assert searcher._cfg.default_headers.get("User-Agent") == "smart-research-agent/1.0"


@pytest.mark.asyncio
async def test_cratesio_searcher_search_success():
    """Parse de crates[] retorna SearchResult corretos."""
    mock_data = {
        "crates": [
            {
                "name": "serde",
                "description": "A generic serialization/deserialization framework",
                "homepage": "https://serde.rs",
                "repository": "https://github.com/serde-rs/serde",
                "newest_version": "1.0.197",
                "downloads": 123456789,
            }
        ]
    }
    searcher = CratesIOSearcher({})
    searcher._make_request = AsyncMock(return_value=mock_data)

    results = await searcher.search("serde")
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].source == "cratesio"
    assert results[0].title == "serde"
    assert results[0].url == "https://serde.rs"
    assert "serialization" in results[0].description
    assert results[0].metrics["version"] == "1.0.197"
    assert results[0].metrics["repository"] == "https://github.com/serde-rs/serde"


@pytest.mark.asyncio
async def test_cratesio_searcher_search_empty():
    """Resposta vazia ou erro retorna lista vazia sem exceção."""
    searcher = CratesIOSearcher({})
    searcher._make_request = AsyncMock(return_value={"crates": []})
    assert await searcher.search("no_such_crate_xyz") == []

    searcher2 = CratesIOSearcher({})
    searcher2._make_request = AsyncMock(side_effect=Exception("boom"))
    assert await searcher2.search("fail") == []


def test_cratesio_searcher_registered():
    """O searcher está registrado via @register_searcher."""
    assert "cratesio" in get_registry()
    assert get_registry()["cratesio"]["cls"] is CratesIOSearcher
