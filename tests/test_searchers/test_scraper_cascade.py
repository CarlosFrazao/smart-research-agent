import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.types import SearchResult


def _make_result(source: str, description: str = "x" * 300) -> SearchResult:
    return SearchResult(
        source=source,
        title=f"{source} title",
        url=f"https://{source}.example.com",
        description=description,
        metrics={},
        raw={},
    )


def _make_orchestrator(spider_enabled: bool = False, steel_enabled: bool = False):
    """Builds a minimal Orchestrator with mocked dependencies."""
    with patch("src.orchestrator.LLMClient"), \
         patch("src.orchestrator.IntentAnalyzer"), \
         patch("src.orchestrator.QueryExpander"), \
         patch("src.orchestrator.SourcePlanner"), \
         patch("src.orchestrator.QualityRanker"), \
         patch("src.orchestrator.GapDetector"), \
         patch("src.orchestrator.Synthesizer"), \
         patch("src.orchestrator.ReportGenerator"), \
         patch("src.orchestrator.Cache"):

        from src.config import Config
        from src.orchestrator import Orchestrator

        config = Config(
            spider_enabled=spider_enabled,
            steel_enabled=steel_enabled,
            spider_api_key="test-spider-key" if spider_enabled else "",
            steel_api_key="test-steel-key" if steel_enabled else "",
            firecrawl_api_key="fc-test",
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = config

        mock_firecrawl = MagicMock()
        mock_firecrawl.search = AsyncMock(return_value=[])
        mock_firecrawl.client = MagicMock()
        mock_firecrawl.client.scrape = AsyncMock(return_value={})

        mock_spider = MagicMock()
        mock_spider.search = AsyncMock(return_value=[])

        mock_steel = MagicMock()
        mock_steel.search = AsyncMock(return_value=[])

        orch.searchers = {"firecrawl": mock_firecrawl}
        if spider_enabled:
            orch.searchers["spider"] = mock_spider
        if steel_enabled:
            orch.searchers["steel"] = mock_steel

        return orch, mock_firecrawl, mock_spider, mock_steel


# ─── Cascade disabled (original behavior) ────────────────────────────────────

@pytest.mark.asyncio
async def test_cascade_disabled_uses_firecrawl_only():
    """When both Spider and Steel are disabled, uses Firecrawl directly."""
    orch, mock_fc, _, _ = _make_orchestrator(spider_enabled=False, steel_enabled=False)
    expected = [_make_result("firecrawl")]
    mock_fc.search = AsyncMock(return_value=expected)

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == expected
    mock_fc.search.assert_called_once_with("https://example.com")


@pytest.mark.asyncio
async def test_cascade_disabled_no_spider_or_steel_call():
    """When cascade is disabled, Spider and Steel are never instantiated or called."""
    orch, mock_fc, mock_spider, mock_steel = _make_orchestrator(
        spider_enabled=False, steel_enabled=False
    )
    mock_fc.search = AsyncMock(return_value=[_make_result("firecrawl")])

    await orch._select_scraper_for_url("https://example.com")

    mock_spider.search.assert_not_called()
    mock_steel.search.assert_not_called()


# ─── Cascade enabled: Firecrawl succeeds ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cascade_firecrawl_success_stops_cascade():
    """When Firecrawl returns rich content (>200 chars), cascade stops there."""
    orch, mock_fc, mock_spider, _ = _make_orchestrator(spider_enabled=True)
    expected = [_make_result("firecrawl", "x" * 300)]
    mock_fc.search = AsyncMock(return_value=expected)

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == expected
    mock_spider.search.assert_not_called()


# ─── Cascade enabled: Firecrawl fails → Spider succeeds ─────────────────────

@pytest.mark.asyncio
async def test_cascade_firecrawl_error_falls_to_spider():
    """Firecrawl error triggers Spider.cloud fallback."""
    orch, mock_fc, mock_spider, _ = _make_orchestrator(spider_enabled=True)
    mock_fc.search = AsyncMock(side_effect=Exception("Firecrawl down"))
    expected = [_make_result("spider.cloud")]
    mock_spider.search = AsyncMock(return_value=expected)

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == expected
    mock_spider.search.assert_called_once()


@pytest.mark.asyncio
async def test_cascade_firecrawl_short_content_falls_to_spider():
    """Firecrawl returns <200 chars content → falls to Spider."""
    orch, mock_fc, mock_spider, _ = _make_orchestrator(spider_enabled=True)
    mock_fc.search = AsyncMock(return_value=[_make_result("firecrawl", "short")])
    expected = [_make_result("spider.cloud")]
    mock_spider.search = AsyncMock(return_value=expected)

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == expected


# ─── Cascade enabled: Firecrawl + Spider fail → Steel succeeds ──────────────

@pytest.mark.asyncio
async def test_cascade_spider_fails_falls_to_steel():
    """Spider failure falls through to Steel.dev."""
    orch, mock_fc, mock_spider, mock_steel = _make_orchestrator(
        spider_enabled=True, steel_enabled=True
    )
    mock_fc.search = AsyncMock(side_effect=Exception("FC down"))
    mock_spider.search = AsyncMock(side_effect=Exception("Spider down"))
    expected = [_make_result("steel.dev")]
    mock_steel.search = AsyncMock(return_value=expected)

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == expected
    mock_steel.search.assert_called_once()


# ─── Cascade enabled: all scrapers fail → Jina Reader ───────────────────────

@pytest.mark.asyncio
async def test_cascade_all_fail_uses_jina():
    """When all scrapers fail, falls back to Jina Reader."""
    orch, mock_fc, mock_spider, mock_steel = _make_orchestrator(
        spider_enabled=True, steel_enabled=True
    )
    mock_fc.search = AsyncMock(side_effect=Exception("FC down"))
    mock_spider.search = AsyncMock(side_effect=Exception("Spider down"))
    mock_steel.search = AsyncMock(side_effect=Exception("Steel down"))
    mock_fc.client.scrape = AsyncMock(return_value={"markdown": "Jina content here"})

    result = await orch._select_scraper_for_url("https://example.com")

    assert len(result) == 1
    assert result[0].source == "jina_reader"
    assert "Jina content" in result[0].description


@pytest.mark.asyncio
async def test_cascade_all_fail_including_jina_returns_empty():
    """When even Jina fails, returns empty list without raising."""
    orch, mock_fc, mock_spider, mock_steel = _make_orchestrator(
        spider_enabled=True, steel_enabled=True
    )
    mock_fc.search = AsyncMock(side_effect=Exception("FC down"))
    mock_spider.search = AsyncMock(side_effect=Exception("Spider down"))
    mock_steel.search = AsyncMock(side_effect=Exception("Steel down"))
    mock_fc.client.scrape = AsyncMock(side_effect=Exception("Jina down"))

    result = await orch._select_scraper_for_url("https://example.com")

    assert result == []
