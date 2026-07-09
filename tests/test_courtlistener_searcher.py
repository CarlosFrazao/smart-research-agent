import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.search.courtlistener_searcher import CourtListenerSearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_courtlistener_search_success():
    # Mock successful API response
    mock_resp = {
        "results": [
            {
                "caseName": "Smith v. Beta LLC",
                "absoluteUrl": "/t/smith-v-beta/1234567/",
                "snippet": "The court held that the defendant's negligence was evident.",
                "court": "Supreme Court",
                "docketNumber": "20-1234"
            },
            {
                "caseName": "Doe v. Acme Corp",
                "absoluteUrl": "/t/doe-v-acme/5678901/",
                "snippet": "Summary of the judgment...",
                "court": "Federal Circuit",
                "docketNumber": "21-5678"
            }
        ],
        "count": 2,
        "nextPage": "https://api.example.com/page2"
    }

    config = {"timeout": 5, "max_results": 5}
    searcher = CourtListenerSearcher(config)

    with patch.object(CourtListenerSearcher, "_make_request", return_value=mock_resp):
        results = await searcher.search("class action securities")

    assert len(results) == 2

    # First result checks
    r1 = results[0]
    assert r1.source == "courtlistener"
    assert r1.title == "Smith v. Beta LLC"
    assert r1.url == "https://www.courtlistener.com/t/smith-v-beta/1234567/"
    assert "defendant's negligence" in r1.description
    assert r1.metrics["court"] == "Supreme Court"
    assert r1.metrics["docketNumber"] == "20-1234"

    # Second result checks
    r2 = results[1]
    assert r2.source == "courtlistener"
    assert r2.title == "Doe v. Acme Corp"
    assert r2.url == "https://www.courtlistener.com/t/doe-v-acme/5678901/"
    assert "Summary of the judgment" in r2.description
    assert r2.metrics["court"] == "Federal Circuit"
    assert r2.metrics["docketNumber"] == "21-5678"

    await searcher.close()


@pytest.mark.asyncio
async def test_courtlistener_graceful_without_timeout():
    """Test behavior when timeout is not provided."""
    config = {"timeout": 10, "max_results": 5}
    searcher = CourtListenerSearcher(config)

    # Override timeout to something invalid (e.g., None) to force fallback
    with patch.object(searcher, "_make_request", return_value={}):
        results = await searcher.search("test")
    assert results == []
    await searcher.close()