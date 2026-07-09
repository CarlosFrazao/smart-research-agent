import pytest
from unittest.mock import patch
from src.search.sec_edgar_searcher import SECEdgarSearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_sec_edgar_search_success():
    # Mock data returned from SEC API - matching actual API structure
    mock_data = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "display_names": "AI Patent Filings Surge",
                        "file_date": "2024-01-01",
                        "period_of_report": "Q1 2024"
                    },
                    "_id": "test-filing-id-20240101"
                }
            ]
        }
    }

    config = {"timeout": 30, "max_results": 5}
    searcher = SECEdgarSearcher(config)

    with patch.object(SECEdgarSearcher, "_make_request", return_value=mock_data):
        results = await searcher.search("AI")

    assert len(results) == 1
    r = results[0]

    assert r.source == "sec_edgar"
    assert "AI Patent Filings Surge" in r.title
    assert "Q1 2024" in r.description
    assert r.metrics["display_names"] == "AI Patent Filings Surge"
    assert r.metrics["file_date"] == "2024-01-01"
    assert r.metrics["filing_id"] == "test-filing-id-20240101"

    await searcher.close()


@pytest.mark.asyncio
async def test_sec_edgar_no_results():
    """When API returns empty hits, we should get an empty list."""
    config = {"timeout": 30, "max_results": 5}
    searcher = SECEdgarSearcher(config)

    empty_response = {"hits": {"hits": []}}
    with patch.object(SECEdgarSearcher, "_make_request", return_value=empty_response):
        results = await searcher.search("non_existent_term")
        assert results == []

    await searcher.close()