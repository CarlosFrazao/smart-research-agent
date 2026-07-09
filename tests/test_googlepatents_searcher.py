import pytest
from unittest.mock import patch
from src.search.googlepatents_searcher import GooglePatentsSearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_googlepatents_search_success():
    # Mock scraped content with metadata structure
    mock_scraped = {
        "metadata": {
            "patent_id": "US12345678B2",
            "title": "Machine Learning System",
            "abstract": "This invention relates to a system for learning from data",
            "url": "https://patents.google.com/patent/US12345678B2/en",
            "assignee": "TechCo Inc.",
            "filing_date": "2023-05-12"
        }
    }

    config = {"timeout": 15, "max_results": 5}
    searcher = GooglePatentsSearcher(config)

    with patch.object(GooglePatentsSearcher, "_scrape_url", return_value=mock_scraped):
        results = await searcher.search("machine learning system")

    # Expect at least one result
    assert len(results) >= 1
    r = results[0]
    assert r.source == "google_patents"
    assert r.title == "Machine Learning System"
    # Check that the abstract is included in the description
    assert "This invention relates to a system for learning from data" in r.description
    assert r.metrics.get("patent_id") == "US12345678B2"
    assert r.metrics.get("assignee") == "TechCo Inc."
    assert r.metrics.get("filing_date") == "2023-05-12"

    await searcher.close()


@pytest.mark.asyncio
async def test_googlepatents_empty_on_scrape_failure():
    config = {"timeout": 15, "max_results": 5}
    searcher = GooglePatentsSearcher(config)

    with patch.object(GooglePatentsSearcher, "_scrape_url", side_effect=Exception("Network error")):
        results = await searcher.search("something")
        assert results == []

    await searcher.close()