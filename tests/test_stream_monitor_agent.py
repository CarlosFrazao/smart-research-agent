"""
test_stream_monitor_agent.py — Testes unitarios para StreamMonitorAgent

Cobre (offline, sem rede):
  1. Parser RSS: bug Element.__bool__ (titulos validos detectados)
  2. Parser Atom: entradas sem filhos retornam texto correto
  3. _parse_webhook_payload: SSE data line valida e invalida
  4. deduplicacao via _seen_hashes
  5. add_feed com source_type invalido levanta ValueError
  6. pause_feed / resume_feed / remove_feed
  7. circuit-breaker: auto-pausa apos MAX_CONSECUTIVE_ERRORS
  8. _prune_expired: poda eventos e hashes expirados
  9. events_as_search_results: campos compativeis com SearchResult
 10. get_report: contagens corretas de active/paused
 11. get_recent_events: filtro de topic e limite
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.stream_monitor_agent import (
    MAX_CONSECUTIVE_ERRORS,
    SUPPORTED_SOURCE_TYPES,
    StreamEvent,
    StreamMonitorAgent,
    _find_link,
    _find_text,
    _parse_webhook_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rss_item(title: str, link: str, description: str = "") -> ET.Element:
    """Constroi um <item> RSS 2.0 com texto real nos subelementos."""
    item = ET.Element("item")
    t = ET.SubElement(item, "title")
    t.text = title
    l = ET.SubElement(item, "link")
    l.text = link
    if description:
        d = ET.SubElement(item, "description")
        d.text = description
    return item


def _make_atom_entry(ns_prefix: str = "atom") -> ET.Element:
    """Constroi uma entrada Atom minima."""
    ns = "http://www.w3.org/2005/Atom"
    entry = ET.Element(f"{{{ns}}}entry")
    t = ET.SubElement(entry, f"{{{ns}}}title")
    t.text = "Atom Title"
    s = ET.SubElement(entry, f"{{{ns}}}summary")
    s.text = "Atom summary text"
    return entry


def _make_event(title: str = "T", url: str = "https://x.com", age: float = 0.0) -> StreamEvent:
    e = StreamEvent(source_type="rss", source_url="http://feed", title=title, url=url)
    e.published_at = time.time() - age
    return e


def _make_agent() -> StreamMonitorAgent:
    return StreamMonitorAgent()


# ---------------------------------------------------------------------------
# Testes: parsers de elemento (sem rede)
# ---------------------------------------------------------------------------

class TestFindText:
    NS = {"atom": "http://www.w3.org/2005/Atom"}

    def test_rss_title_with_text_is_found(self):
        """Bug corrigido: elemento com texto mas sem filhos deve retornar o texto."""
        item = _make_rss_item("My Title", "https://example.com")
        result = _find_text(item, "title", "atom:title", self.NS)
        assert result == "My Title"

    def test_atom_fallback_when_rss_tag_absent(self):
        """Fallback para atom:title quando nao existe <title> RSS."""
        ns_uri = "http://www.w3.org/2005/Atom"
        entry = ET.Element("entry")
        t = ET.SubElement(entry, f"{{{ns_uri}}}title")
        t.text = "Atom Fallback"
        result = _find_text(entry, "title", "atom:title", self.NS)
        assert result == "Atom Fallback"

    def test_returns_empty_when_tag_absent(self):
        item = ET.Element("item")  # sem subelementos
        result = _find_text(item, "title", "atom:title", self.NS)
        assert result == ""

    def test_strips_whitespace(self):
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = "  Spaced Title  "
        result = _find_text(item, "title", "atom:title", self.NS)
        assert result == "Spaced Title"


class TestFindLink:
    NS = {"atom": "http://www.w3.org/2005/Atom"}

    def test_rss_link_with_text(self):
        item = _make_rss_item("T", "https://rss-link.com")
        assert _find_link(item, self.NS) == "https://rss-link.com"

    def test_atom_link_with_href(self):
        ns_uri = "http://www.w3.org/2005/Atom"
        entry = ET.Element("entry")
        l = ET.SubElement(entry, f"{{{ns_uri}}}link")
        l.set("href", "https://atom-href.com")
        ns = {"atom": ns_uri}
        # _find_link tenta "link" (sem ns) primeiro, depois "atom:link"
        # Para atom puro, o elemento tem namespace
        result = _find_link(entry, ns)
        assert result == "https://atom-href.com"

    def test_returns_empty_when_no_link(self):
        item = ET.Element("item")
        assert _find_link(item, self.NS) == ""


# ---------------------------------------------------------------------------
# Testes: _parse_webhook_payload
# ---------------------------------------------------------------------------

class TestParseWebhookPayload:
    def test_valid_payload_returns_event(self):
        raw = '{"title": "New Release", "url": "https://r.com/v1", "summary": "Details"}'
        event = _parse_webhook_payload(raw, "https://hook.io", ["AI"])
        assert event is not None
        assert event.title == "New Release"
        assert event.url == "https://r.com/v1"
        assert event.summary == "Details"
        assert event.source_type == "webhook"

    def test_missing_title_returns_none(self):
        raw = '{"url": "https://r.com"}'
        assert _parse_webhook_payload(raw, "https://hook.io", []) is None

    def test_missing_url_returns_none(self):
        raw = '{"title": "Something"}'
        assert _parse_webhook_payload(raw, "https://hook.io", []) is None

    def test_invalid_json_returns_none(self):
        assert _parse_webhook_payload("not json at all", "https://hook.io", []) is None

    def test_summary_truncated_at_500(self):
        long_summary = "x" * 600
        raw = f'{{"title": "T", "url": "https://u.com", "summary": "{long_summary}"}}'
        event = _parse_webhook_payload(raw, "https://hook.io", [])
        assert event is not None
        assert len(event.summary) <= 500


# ---------------------------------------------------------------------------
# Testes: StreamMonitorAgent (offline)
# ---------------------------------------------------------------------------

class TestAddFeed:
    def test_valid_source_types_accepted(self):
        agent = _make_agent()
        for stype in SUPPORTED_SOURCE_TYPES:
            feed = agent.add_feed(f"f_{stype}", "url", stype)
            assert feed.source_type == stype

    def test_invalid_source_type_raises(self):
        agent = _make_agent()
        with pytest.raises(ValueError, match="source_type inv"):
            agent.add_feed("bad", "url", "twitter")

    def test_returns_monitoring_feed_object(self):
        agent = _make_agent()
        feed = agent.add_feed("HN", "https://hn.rss", "rss", ["AI"])
        assert feed.name == "HN"
        assert feed.topics == ["AI"]
        assert feed.is_active is True

    def test_active_feeds_count_updates(self):
        agent = _make_agent()
        agent.add_feed("f1", "u1", "rss")
        agent.add_feed("f2", "u2", "github")
        assert agent.get_report().active_feeds == 2


class TestPauseResumeFeed:
    def test_pause_feed_disables_is_active(self):
        agent = _make_agent()
        agent.add_feed("F1", "u", "rss")
        result = agent.pause_feed("F1")
        assert result is True
        assert agent._feeds[0].is_active is False

    def test_pause_nonexistent_returns_false(self):
        agent = _make_agent()
        assert agent.pause_feed("ghost") is False

    def test_resume_feed_reactivates_and_resets_errors(self):
        agent = _make_agent()
        agent.add_feed("F1", "u", "rss")
        agent._feeds[0].is_active = False
        agent._feeds[0].consecutive_errors = 3
        result = agent.resume_feed("F1")
        assert result is True
        assert agent._feeds[0].is_active is True
        assert agent._feeds[0].consecutive_errors == 0

    def test_remove_feed_deletes_from_list(self):
        agent = _make_agent()
        agent.add_feed("F1", "u", "rss")
        result = agent.remove_feed("F1")
        assert result is True
        assert len(agent._feeds) == 0

    def test_remove_nonexistent_returns_false(self):
        agent = _make_agent()
        assert agent.remove_feed("ghost") is False


class TestCircuitBreaker:
    def test_auto_pauses_after_max_errors(self):
        agent = _make_agent()
        feed = agent.add_feed("F1", "u", "rss")
        for _ in range(MAX_CONSECUTIVE_ERRORS):
            agent._register_feed_error(feed, RuntimeError("err"))
        assert feed.is_active is False
        assert feed.consecutive_errors == MAX_CONSECUTIVE_ERRORS

    def test_does_not_pause_before_max_errors(self):
        agent = _make_agent()
        feed = agent.add_feed("F1", "u", "rss")
        for _ in range(MAX_CONSECUTIVE_ERRORS - 1):
            agent._register_feed_error(feed, RuntimeError("err"))
        assert feed.is_active is True


class TestPruneExpired:
    def test_prune_removes_old_events_and_hashes(self):
        agent = _make_agent()
        # Evento recente e evento expirado
        fresh = _make_event("Fresh", "https://f.com", age=10)
        old = _make_event("Old", "https://o.com", age=7200)  # 2h -> expirado
        agent._events = [fresh, old]
        agent._seen_hashes = {
            fresh.event_hash: fresh.published_at,
            old.event_hash: old.published_at,
        }
        agent._prune_expired()
        assert len(agent._events) == 1
        assert agent._events[0].title == "Fresh"
        assert old.event_hash not in agent._seen_hashes
        assert fresh.event_hash in agent._seen_hashes


class TestIngestEvents:
    def test_deduplication_prevents_duplicate_events(self):
        agent = _make_agent()
        feed = agent.add_feed("F1", "u", "rss")
        event = _make_event("E", "https://u.com")
        agent._ingest_events(feed, [event])
        agent._ingest_events(feed, [event])  # segunda ingestao do mesmo evento
        assert len(agent._events) == 1
        assert agent._report.deduplicated_events == 1

    def test_buffer_fifo_enforces_max_events(self):
        from src.stream_monitor_agent import MAX_EVENTS_BUFFER
        agent = _make_agent()
        feed = agent.add_feed("F1", "u", "rss")
        events = [_make_event(f"E{i}", f"https://u.com/{i}") for i in range(MAX_EVENTS_BUFFER + 5)]
        agent._ingest_events(feed, events)
        assert len(agent._events) <= MAX_EVENTS_BUFFER


class TestGetRecentEvents:
    def test_returns_events_sorted_newest_first(self):
        agent = _make_agent()
        old = _make_event("Old", "https://o.com", age=100)
        new = _make_event("New", "https://n.com", age=1)
        agent._events = [old, new]
        recent = agent.get_recent_events()
        assert recent[0].title == "New"

    def test_topic_filter(self):
        agent = _make_agent()
        e1 = _make_event("AI Paper", "https://a.com")
        e1.topic_tags = ["AI", "ML"]
        e2 = _make_event("Sports", "https://s.com")
        e2.topic_tags = ["sports"]
        agent._events = [e1, e2]
        filtered = agent.get_recent_events(topic_filter="AI")
        assert len(filtered) == 1
        assert filtered[0].title == "AI Paper"

    def test_limit_respected(self):
        agent = _make_agent()
        agent._events = [_make_event(f"E{i}", f"https://u.com/{i}") for i in range(20)]
        assert len(agent.get_recent_events(limit=5)) == 5


class TestGetReport:
    def test_report_paused_count(self):
        agent = _make_agent()
        agent.add_feed("F1", "u", "rss")
        agent.add_feed("F2", "u2", "github")
        agent.pause_feed("F1")
        report = agent.get_report()
        assert report.active_feeds == 1
        assert report.paused_feeds == 1


@pytest.mark.asyncio
async def test_events_as_search_results_schema():
    """events_as_search_results() produz SearchResult com campos corretos."""
    agent = _make_agent()
    event = _make_event("Paper Title", "https://paper.com")
    event.summary = "Great paper"
    event.relevance_score = 0.8
    event.source_type = "arxiv"
    agent._events = [event]

    results = await agent.events_as_search_results(limit=1)
    assert len(results) == 1

    from src.types import SearchResult
    result = results[0]
    assert isinstance(result, SearchResult)
    assert result.title == "Paper Title"
    assert result.url == "https://paper.com"
    assert result.description == "Great paper"
    assert "stream_monitor" in result.source
    assert 0.0 <= result.confidence_score <= 1.0
    assert result.metrics.get("real_time") is True
    assert result.metrics.get("topic_tags") is not None
