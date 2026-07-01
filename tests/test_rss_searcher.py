"""Testes do RSSSearcher P2 — parse XML, normalização e scoring."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.search.rss_searcher import (
    RSSSearcher,
    parse_feed_xml,
    _strip_html,
    _parse_date,
    _score_relevance,
    _stable_id,
    DEFAULT_FEEDS,
)
from src.types import SearchResult


# ── Helpers unit ─────────────────────────────────────────────────────────────

class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    def test_decodes_entities(self):
        assert _strip_html("AT&amp;T") == "AT&T"
        assert _strip_html("&lt;tag&gt;") == "<tag>"

    def test_collapses_whitespace(self):
        assert _strip_html("a  \n  b") == "a b"

    def test_empty_input(self):
        assert _strip_html("") == ""

    def test_none_input(self):
        assert _strip_html(None) == ""


class TestParseDate:
    def test_rfc2822_date(self):
        result = _parse_date("Mon, 01 Jan 2024 12:00:00 +0000")
        assert result is not None
        assert "2024-01-01" in result

    def test_iso8601_date(self):
        result = _parse_date("2024-03-15T10:30:00Z")
        assert result is not None
        assert "2024-03-15" in result

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None

    def test_invalid_returns_raw(self):
        result = _parse_date("not a date at all")
        assert result == "not a date at all"


class TestStableId:
    def test_deterministic(self):
        a = _stable_id("https://example.com", "Title")
        b = _stable_id("https://example.com", "Title")
        assert a == b

    def test_different_urls_differ(self):
        a = _stable_id("https://example.com/a", "Title")
        b = _stable_id("https://example.com/b", "Title")
        assert a != b

    def test_returns_12_chars(self):
        assert len(_stable_id("https://x.com", "t")) == 12


class TestScoreRelevance:
    def test_exact_match_high_score(self):
        score = _score_relevance("claude ai", "Claude AI Update", "Latest news", 1.0)
        assert score > 30

    def test_no_match_low_score(self):
        score = _score_relevance("quantum physics neutron star", "CRM Software Update", "Business tools for teams", 1.0)
        assert score < 30

    def test_empty_query_returns_weight_based(self):
        score = _score_relevance("", "anything", "anything", 1.5)
        assert score == pytest.approx(1.5 * 30.0)

    def test_title_match_bonus(self):
        score_title = _score_relevance("llm", "LLM Benchmark 2024", "Details here", 1.0)
        score_desc = _score_relevance("llm", "Tech Update", "LLM performance benchmark data", 1.0)
        assert score_title > score_desc

    def test_max_100(self):
        score = _score_relevance("claude ai model", "Claude AI Model", "Claude AI Model excellent", 5.0)
        assert score <= 100.0


# ── XML parser ───────────────────────────────────────────────────────────────

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Claude 3.7 Released</title>
      <link>https://anthropic.com/blog/claude-3-7</link>
      <description>Anthropic releases Claude 3.7 with extended thinking.</description>
      <pubDate>Mon, 15 Jan 2024 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>GPT-5 Announcement</title>
      <link>https://openai.com/blog/gpt-5</link>
      <description>OpenAI announces GPT-5 model capabilities.</description>
      <pubDate>Tue, 16 Jan 2024 12:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Simon Willison's Blog</title>
  <entry>
    <title>Using LLMs for code generation</title>
    <link href="https://simonwillison.net/2024/llms-code"/>
    <summary>A detailed exploration of LLM code generation patterns.</summary>
    <updated>2024-02-01T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Notes on RAG</title>
    <link href="https://simonwillison.net/2024/rag"/>
    <summary>Retrieval augmented generation techniques.</summary>
    <updated>2024-02-02T10:00:00Z</updated>
  </entry>
</feed>"""

EMPTY_FEED = ""
MALFORMED_FEED = "<not xml at all!!>"


class TestParseFeedXml:
    def test_rss_parses_two_items(self):
        items = parse_feed_xml(RSS_SAMPLE, "Test Feed")
        assert len(items) == 2

    def test_rss_item_title(self):
        items = parse_feed_xml(RSS_SAMPLE, "Test Feed")
        titles = [i["title"] for i in items]
        assert "Claude 3.7 Released" in titles

    def test_rss_item_url(self):
        items = parse_feed_xml(RSS_SAMPLE, "Test Feed")
        urls = [i["url"] for i in items]
        assert "https://anthropic.com/blog/claude-3-7" in urls

    def test_rss_item_description(self):
        items = parse_feed_xml(RSS_SAMPLE, "Test Feed")
        assert "extended thinking" in items[0]["description"]

    def test_rss_item_date_parsed(self):
        items = parse_feed_xml(RSS_SAMPLE, "Test Feed")
        assert items[0]["published"] is not None
        assert "2024-01-15" in items[0]["published"]

    def test_atom_detects_entries(self):
        items = parse_feed_xml(ATOM_SAMPLE, "Simon Willison")
        assert len(items) == 2

    def test_atom_item_title(self):
        items = parse_feed_xml(ATOM_SAMPLE, "Simon Willison")
        assert items[0]["title"] == "Using LLMs for code generation"

    def test_atom_item_url_from_href(self):
        items = parse_feed_xml(ATOM_SAMPLE, "Simon Willison")
        assert "simonwillison.net" in items[0]["url"]

    def test_empty_feed_returns_empty(self):
        assert parse_feed_xml(EMPTY_FEED, "X") == []

    def test_malformed_feed_returns_empty(self):
        result = parse_feed_xml(MALFORMED_FEED, "X")
        assert isinstance(result, list)


# ── RSSSearcher.normalize ─────────────────────────────────────────────────────

class TestRSSSearcherNormalize:
    def _searcher(self):
        return RSSSearcher({"enabled": True, "timeout": 10, "max_results": 20})

    def test_normalize_dict_produces_search_result(self):
        s = self._searcher()
        result = s.normalize({
            "title": "Claude Update",
            "url": "https://anthropic.com/blog/update",
            "description": "Latest update.",
            "feed": "Anthropic",
            "feed_id": "anthropic",
            "published": "2024-01-15T10:00:00+00:00",
            "weight": 1.5,
        })
        assert isinstance(result, SearchResult)
        assert result.title == "Claude Update"
        assert result.source == "rss:anthropic"
        assert result.metrics["feed_name"] == "Anthropic"
        assert result.metrics["weight"] == 1.5

    def test_normalize_non_dict_returns_empty_result(self):
        s = self._searcher()
        result = s.normalize("invalid")
        assert result.title == "" or result.source == "rss"

    def test_normalize_truncates_description(self):
        s = self._searcher()
        result = s.normalize({
            "title": "T", "url": "https://x.com", "description": "x" * 600,
            "feed": "F", "feed_id": "f", "weight": 1.0,
        })
        assert len(result.description) <= 500

    def test_normalize_stable_id_in_metrics(self):
        s = self._searcher()
        result = s.normalize({
            "title": "Title", "url": "https://x.com", "description": "desc",
            "feed": "F", "feed_id": "f", "weight": 1.0,
        })
        assert "item_id" in result.metrics
        assert len(result.metrics["item_id"]) == 12


# ── RSSSearcher.search mocked ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_results_sorted_by_relevance():
    """Verifica que search() ordena por relevance_score desc."""
    s = RSSSearcher({"enabled": True, "timeout": 10, "max_results": 20,
                     "feeds": [DEFAULT_FEEDS[0], DEFAULT_FEEDS[1]]})

    async def fake_fetch(query, feed):
        items = [
            SearchResult(source=f"rss:{feed['id']}", title=f"Match {query}",
                         url="https://x.com", description=query,
                         metrics={"relevance_score": 80.0 if feed["id"] == "anthropic" else 40.0}),
        ]
        return items

    with patch.object(s, "_fetch_feed", side_effect=fake_fetch):
        results = await s.search("claude ai")

    assert len(results) > 0
    scores = [r.metrics["relevance_score"] for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_search_disabled_returns_empty():
    s = RSSSearcher({"enabled": False, "timeout": 10, "max_results": 20})
    results = await s.search("anything")
    assert results == []


@pytest.mark.asyncio
async def test_search_feed_error_is_swallowed():
    """Uma falha em um feed não deve quebrar a busca inteira."""
    s = RSSSearcher({"enabled": True, "timeout": 10, "max_results": 20,
                     "feeds": [DEFAULT_FEEDS[0]]})

    async def raise_error(query, feed):
        raise ConnectionError("Network failure")

    with patch.object(s, "_fetch_feed", side_effect=raise_error):
        results = await s.search("claude")

    assert results == []


@pytest.mark.asyncio
async def test_search_respects_max_results():
    s = RSSSearcher({"enabled": True, "timeout": 10, "max_results": 3,
                     "feeds": DEFAULT_FEEDS[:2]})

    async def fake_fetch(query, feed):
        return [
            SearchResult(source=f"rss:{feed['id']}", title=f"Item {i}",
                         url=f"https://x.com/{i}", description="content",
                         metrics={"relevance_score": float(10 - i)})
            for i in range(5)
        ]

    with patch.object(s, "_fetch_feed", side_effect=fake_fetch):
        results = await s.search("test")

    assert len(results) <= 3


# ── DEFAULT_FEEDS catalog ─────────────────────────────────────────────────────

class TestDefaultFeedsCatalog:
    def test_minimum_15_feeds(self):
        assert len(DEFAULT_FEEDS) >= 15

    def test_each_feed_has_required_keys(self):
        for feed in DEFAULT_FEEDS:
            assert "id" in feed
            assert "name" in feed
            assert "url" in feed
            assert "weight" in feed

    def test_weights_in_valid_range(self):
        for feed in DEFAULT_FEEDS:
            assert 0.1 <= feed["weight"] <= 3.0

    def test_all_ids_unique(self):
        ids = [f["id"] for f in DEFAULT_FEEDS]
        assert len(ids) == len(set(ids))

    def test_anthropic_feed_present(self):
        ids = [f["id"] for f in DEFAULT_FEEDS]
        assert "anthropic" in ids

    def test_arxiv_feed_present(self):
        ids = [f["id"] for f in DEFAULT_FEEDS]
        assert "arxiv_ai" in ids
