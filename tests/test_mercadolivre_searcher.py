import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.search.mercadolivre_searcher import MercadoLivreSearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_mercadolivre_search_success():
    # Mock successful API response
    mock_resp = {
        "results": [
            {
                "title": "Wireless Headphones",
                "permalink": "https://www.mercadolivre.com.br/wireless-headphones#MLB-123456789",
                "price": 299.99,
                "currency_id": "BRL",
                "condition": "new",
                "thumbnail": "https://img.mercadolibre.com/BzR.jpg",
                "description": "High-quality wireless headphones with noise-cancellation."
            },
            {
                "title": "Smart Watch",
                "permalink": "https://www.mercadolivre.com.br/smart-watch#MLB-987654321",
                "price": 399.90,
                "currency_id": "BRL",
                "condition": "new",
                "thumbnail": "https://img.mercadolibre.com/abcde.jpg",
                "description": "Multi-functional smartwatch with heart-rate monitoring."
            }
        ]
    }

    config = {"timeout": 20, "max_results": 5}
    searcher = MercadoLivreSearcher(config)

    with patch.object(MercadoLivreSearcher, "_make_request", return_value=mock_resp):
        results = await searcher.search("wireless headphones")

    assert len(results) == 2
    r1 = results[0]
    assert r1.source == "mercadolivre"
    assert r1.title == "Wireless Headphones"
    assert r1.url == "https://www.mercadolivre.com.br/wireless-headphones#MLB-123456789"
    # Check that description contains the formatted price info
    assert "R$ 299.99 (new)" in r1.description or "R$ 299,99 (new)" in r1.description
    assert r1.metrics["price"] == 299.99
    assert r1.metrics["currency_id"] == "BRL"
    assert r1.metrics["condition"] == "new"

    await searcher.close()


@pytest.mark.asyncio
async def test_mercadolivre_fallback_empty_on_error():
    """Edge case: when _make_request raises, we should receive empty results."""
    config = {"timeout": 10, "max_results": 5}
    searcher = MercadoLivreSearcher(config)

    with patch.object(searcher, "_make_request", side_effect=Exception("timeout")):
        results = await searcher.search("any")
        assert results == []

    await searcher.close()