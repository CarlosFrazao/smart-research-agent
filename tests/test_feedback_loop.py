"""Testes do Feedback Loop P3 — FeedbackStore, FeedbackRanker, MCP tool."""
import json
import pytest
import tempfile
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.feedback_store import FeedbackStore, VALID_SIGNALS
from src.feedback_ranker import FeedbackRanker, _result_id
from src.types import SynthesizedResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _tmp_store() -> FeedbackStore:
    tmp = tempfile.mktemp(suffix=".jsonl")
    return FeedbackStore(store_path=tmp)


def _make_result(title: str, entity: str = "", score: float = 70.0) -> SynthesizedResult:
    return SynthesizedResult(
        entity=entity or title.lower().split()[0],
        title=title,
        description=f"Description of {title}",
        sources=["github"],
        urls=[f"https://github.com/test/{title.lower().replace(' ', '-')}"],
        combined_score=score,
        metrics={},
        highlights=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
    )


# ── FeedbackStore ─────────────────────────────────────────────────────────────

class TestFeedbackStore:
    def test_record_creates_file(self):
        store = _tmp_store()
        store.record("abc123", "useful")
        assert store.path.exists()

    def test_record_returns_entry_dict(self):
        store = _tmp_store()
        entry = store.record("abc123", "useful", query="test query")
        assert entry["result_id"] == "abc123"
        assert entry["signal"] == "useful"
        assert "timestamp" in entry

    def test_record_empty_id_raises(self):
        store = _tmp_store()
        with pytest.raises(ValueError, match="result_id"):
            store.record("", "useful")

    def test_record_invalid_signal_raises(self):
        store = _tmp_store()
        with pytest.raises(ValueError, match="signal inválido"):
            store.record("abc123", "banana")

    def test_all_valid_signals_accepted(self):
        store = _tmp_store()
        for sig in VALID_SIGNALS:
            store.record(f"id_{sig}", sig)
        records = store.load_all()
        assert len(records) == len(VALID_SIGNALS)

    def test_load_all_empty_file(self):
        store = _tmp_store()
        assert store.load_all() == []

    def test_load_all_reads_back_records(self):
        store = _tmp_store()
        store.record("id1", "useful", "q1")
        store.record("id2", "bookmark", "q2")
        records = store.load_all()
        assert len(records) == 2
        assert records[0]["result_id"] == "id1"
        assert records[1]["signal"] == "bookmark"

    def test_load_all_skips_malformed_lines(self, tmp_path):
        fpath = tmp_path / "fb.jsonl"
        fpath.write_text('{"result_id": "a", "signal": "useful", "query": "", "timestamp": "t"}\n{INVALID}\n')
        store = FeedbackStore(store_path=str(fpath))
        records = store.load_all()
        assert len(records) == 1

    def test_get_scores_positive(self):
        store = _tmp_store()
        store.record("id1", "useful")
        store.record("id1", "bookmark")
        scores = store.get_scores()
        assert scores["id1"] == pytest.approx(3.5)  # 1.5 + 2.0

    def test_get_scores_negative(self):
        store = _tmp_store()
        store.record("id2", "irrelevant")
        store.record("id2", "not_useful")
        scores = store.get_scores()
        assert scores["id2"] == pytest.approx(-2.5)  # -1.5 + -1.0

    def test_get_scores_empty(self):
        store = _tmp_store()
        assert store.get_scores() == {}

    def test_get_scores_multiple_ids(self):
        store = _tmp_store()
        store.record("id1", "useful")
        store.record("id2", "irrelevant")
        scores = store.get_scores()
        assert "id1" in scores
        assert "id2" in scores
        assert scores["id1"] > 0
        assert scores["id2"] < 0

    def test_clear_returns_count_and_removes(self):
        store = _tmp_store()
        store.record("id1", "useful")
        store.record("id2", "bookmark")
        n = store.clear()
        assert n == 2
        assert not store.path.exists()

    def test_valid_signals_set(self):
        assert "useful" in VALID_SIGNALS
        assert "bookmark" in VALID_SIGNALS
        assert "not_useful" in VALID_SIGNALS
        assert "irrelevant" in VALID_SIGNALS
        assert "outdated" in VALID_SIGNALS


# ── _result_id helper ─────────────────────────────────────────────────────────

class TestResultId:
    def test_deterministic(self):
        r = _make_result("Twenty CRM", entity="twenty")
        assert _result_id(r) == _result_id(r)

    def test_different_titles_differ(self):
        r1 = _make_result("Twenty CRM", entity="twenty")
        r2 = _make_result("Odoo ERP", entity="odoo")
        assert _result_id(r1) != _result_id(r2)

    def test_returns_12_chars(self):
        r = _make_result("Test Project")
        assert len(_result_id(r)) == 12


# ── FeedbackRanker ────────────────────────────────────────────────────────────

class TestFeedbackRanker:
    def test_no_feedback_returns_same_order(self):
        store = _tmp_store()
        ranker = FeedbackRanker(store=store)
        results = [_make_result("A", score=80.0), _make_result("B", score=60.0)]
        out = ranker.apply(results)
        assert [r.title for r in out] == ["A", "B"]

    def test_positive_feedback_boosts_score(self):
        store = _tmp_store()
        r = _make_result("Good Project", entity="good", score=50.0)
        rid = _result_id(r)
        store.record(rid, "bookmark")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert out[0].combined_score > 50.0

    def test_negative_feedback_reduces_score(self):
        store = _tmp_store()
        r = _make_result("Bad Project", entity="bad", score=70.0)
        rid = _result_id(r)
        store.record(rid, "irrelevant")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert out[0].combined_score < 70.0

    def test_delta_capped_at_15(self):
        store = _tmp_store()
        r = _make_result("Starred", entity="starred", score=50.0)
        rid = _result_id(r)
        for _ in range(20):
            store.record(rid, "bookmark")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert out[0].combined_score <= 65.0  # 50 + 15 cap

    def test_score_never_below_zero(self):
        store = _tmp_store()
        r = _make_result("Hated", entity="hated", score=5.0)
        rid = _result_id(r)
        for _ in range(20):
            store.record(rid, "irrelevant")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert out[0].combined_score >= 0.0

    def test_score_never_above_100(self):
        store = _tmp_store()
        r = _make_result("Perfect", entity="perfect", score=98.0)
        rid = _result_id(r)
        for _ in range(20):
            store.record(rid, "useful")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert out[0].combined_score <= 100.0

    def test_reranks_by_adjusted_score(self):
        """High Score (50) com muitos negativos cai abaixo de Low Score (80) com muitos positivos."""
        store = _tmp_store()
        r_high = _make_result("High Score", entity="high", score=50.0)
        r_low = _make_result("Low Score", entity="low", score=80.0)
        rid_high = _result_id(r_high)
        rid_low = _result_id(r_low)
        # "High Score" recebe penalidade: -1.5 -1.5 -1.5 = -4.5 * 5 = -22.5 → capped -15 → 35.0
        for _ in range(3):
            store.record(rid_high, "irrelevant")
        # "Low Score" recebe bônus: +2.0 +2.0 +2.0 = +6.0 * 5 = +30 → capped +15 → 95.0
        for _ in range(3):
            store.record(rid_low, "bookmark")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r_high, r_low])
        assert out[0].title == "Low Score"

    def test_empty_list_returns_empty(self):
        ranker = FeedbackRanker(store=_tmp_store())
        assert ranker.apply([]) == []

    def test_result_id_for_matches_internal(self):
        store = _tmp_store()
        ranker = FeedbackRanker(store=store)
        r = _make_result("Test Project", entity="test")
        assert ranker.result_id_for(r) == _result_id(r)

    def test_original_object_not_mutated(self):
        store = _tmp_store()
        r = _make_result("Immutable", entity="immutable", score=50.0)
        rid = _result_id(r)
        store.record(rid, "useful")
        ranker = FeedbackRanker(store=store)
        out = ranker.apply([r])
        assert r.combined_score == 50.0  # original unchanged
        assert out[0].combined_score != 50.0  # copy changed


# ── MCP tool record_feedback ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_record_feedback_valid():
    """Tool retorna JSON com recorded=True para sinal válido."""
    from unittest.mock import patch as _patch
    with _patch("src.feedback_store.FeedbackStore.record") as mock_record:
        mock_record.return_value = {
            "result_id": "abc123def456",
            "signal": "useful",
            "query": "test",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        import importlib, src.mcp_server as ms
        if not hasattr(ms, "mcp"):
            pytest.skip("MCP FastMCP não disponível neste ambiente")

        from src.mcp_server import record_feedback
        result = await record_feedback(result_id="abc123def456", signal="useful", query="test")
        data = json.loads(result)
        assert data["recorded"] is True
        assert data["signal"] == "useful"


@pytest.mark.asyncio
async def test_mcp_record_feedback_invalid_signal():
    """Tool retorna recorded=False com lista de sinais válidos para sinal inválido."""
    try:
        from src.mcp_server import record_feedback
    except Exception:
        pytest.skip("MCP FastMCP não disponível neste ambiente")

    result = await record_feedback(result_id="abc123", signal="unknown_signal")
    data = json.loads(result)
    assert data["recorded"] is False
    assert "valid_signals" in data


@pytest.mark.asyncio
async def test_mcp_record_feedback_empty_id():
    """Tool retorna recorded=False para result_id vazio."""
    try:
        from src.mcp_server import record_feedback
    except Exception:
        pytest.skip("MCP FastMCP não disponível neste ambiente")

    result = await record_feedback(result_id="", signal="useful")
    data = json.loads(result)
    assert data["recorded"] is False
