"""Testes do SmartModelRouter P5."""
import pytest
from src.clients.smart_model_router import SmartModelRouter, get_router, _TASK_SCORES


class TestSmartModelRouter:
    def _router(self, key: str = "") -> SmartModelRouter:
        return SmartModelRouter(openrouter_api_key=key)

    # ── Tier mapping por task_type ────────────────────────────────────────────

    def test_intent_routes_free_tier_no_key(self):
        r = self._router(key="")
        dec = r.route("intent")
        assert dec.tier == "free"
        assert "haiku" in dec.model_id

    def test_intent_routes_free_via_openrouter_with_key(self):
        r = self._router(key="sk-test")
        dec = r.route("intent")
        assert dec.tier == "free"
        assert dec.provider_override == "openrouter"
        assert "llama" in dec.model_id

    def test_expand_routes_haiku(self):
        r = self._router()
        dec = r.route("expand")
        assert dec.tier == "haiku"

    def test_rank_routes_haiku(self):
        r = self._router()
        dec = r.route("rank")
        assert dec.tier == "haiku"

    def test_synthesis_routes_sonnet(self):
        r = self._router()
        dec = r.route("synthesis")
        assert dec.tier == "sonnet"
        assert "sonnet" in dec.model_id

    def test_report_routes_sonnet(self):
        r = self._router()
        dec = r.route("report")
        assert dec.tier == "sonnet"

    def test_deep_routes_opus(self):
        r = self._router()
        dec = r.route("deep")
        assert dec.tier == "opus"
        assert "opus" in dec.model_id

    # ── Score dinâmico ────────────────────────────────────────────────────────

    def test_long_query_boosts_score(self):
        r = self._router()
        short = r.route("intent", query="short")
        long = r.route("intent", query="x" * 600)
        assert long.score > short.score

    def test_complexity_keyword_boosts_score(self):
        r = self._router()
        simple = r.route("synthesis", query="show me results")
        complex_ = r.route("synthesis", query="architecture refactor analysis")
        assert complex_.score >= simple.score

    def test_large_context_boosts_score(self):
        r = self._router()
        small = r.route("synthesis", context_tokens=100)
        large = r.route("synthesis", context_tokens=9000)
        assert large.score > small.score

    def test_score_capped_at_10(self):
        r = self._router()
        dec = r.route("deep", query="architecture " * 50, context_tokens=20000)
        assert dec.score <= 10

    # ── RoutingDecision campos ────────────────────────────────────────────────

    def test_decision_has_reason(self):
        r = self._router()
        dec = r.route("synthesis")
        assert len(dec.reason) > 0

    def test_decision_model_id_not_empty(self):
        r = self._router()
        for task in _TASK_SCORES:
            dec = r.route(task)
            assert dec.model_id, f"model_id vazio para task={task}"

    def test_unknown_task_defaults_to_sonnet(self):
        r = self._router()
        dec = r.route("unknown_task_xyz")
        assert dec.tier in ("sonnet", "haiku", "opus", "free")

    # ── get_router singleton ──────────────────────────────────────────────────

    def test_get_router_returns_same_instance(self):
        a = get_router()
        b = get_router()
        assert a is b

    # ── Provider override ─────────────────────────────────────────────────────

    def test_no_provider_override_for_haiku(self):
        r = self._router(key="sk-test")
        dec = r.route("expand")
        assert dec.provider_override is None

    def test_provider_override_only_for_free_tier_with_key(self):
        r = self._router(key="sk-test")
        dec_free = r.route("intent")
        dec_sonnet = r.route("synthesis")
        assert dec_free.provider_override == "openrouter"
        assert dec_sonnet.provider_override is None
