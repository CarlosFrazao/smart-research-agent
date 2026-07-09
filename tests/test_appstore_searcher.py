import pytest
from unittest.mock import patch
from src.search.appstore_searcher import AppStoreSearcher
from src.types import SearchResult


@pytest.mark.asyncio
async def test_appstore_search_success():
    mock_data = {
        "resultCount": 2,
        "results": [
            {
                "trackName": "Notion - Notes, Docs, Tasks",
                "trackViewUrl": "https://apps.apple.com/br/app/notion/id123",
                "description": "Notion is the all-in-one workspace.",
                "averageUserRating": 4.7,
                "primaryGenreName": "Productivity",
                "price": 0.0,
                "artistName": "Notion Labs",
                "releaseDate": "2018-03-01T00:00:00Z",
            },
            {
                "trackName": "Paid App",
                "trackViewUrl": "https://apps.apple.com/br/app/paid/id456",
                "description": "A premium application.",
                "averageUserRating": 4.2,
                "primaryGenreName": "Utilities",
                "price": 9.99,
                "artistName": "Dev Co",
                "releaseDate": "2020-06-15T00:00:00Z",
            },
        ],
    }

    config = {"timeout": 10, "max_results": 5}
    searcher = AppStoreSearcher(config)

    with patch.object(searcher, "_make_request", return_value=mock_data):
        results = await searcher.search("notion")

    assert len(results) == 2

    r1 = results[0]
    assert r1.source == "appstore"
    assert r1.title == "Notion - Notes, Docs, Tasks"
    assert r1.url == "https://apps.apple.com/br/app/notion/id123"
    assert "all-in-one workspace" in r1.description
    assert r1.metrics["average_user_rating"] == 4.7
    assert r1.metrics["primary_genre"] == "Productivity"
    assert r1.metrics["price"] == 0.0

    r2 = results[1]
    assert r2.title == "Paid App"
    assert r2.metrics["price"] == 9.99

    await searcher.close()


@pytest.mark.asyncio
async def test_appstore_empty_on_error():
    config = {"timeout": 10, "max_results": 5}
    searcher = AppStoreSearcher(config)

    with patch.object(searcher, "_make_request", side_effect=Exception("network error")):
        results = await searcher.search("any")
        assert results == []

    await searcher.close()