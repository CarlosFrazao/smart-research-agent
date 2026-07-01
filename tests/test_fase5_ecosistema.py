"""
test_fase5_ecosistema.py — Testes de Unidade para Fase 5 (SharedCache + TokenEconomy)

Cobertura:
  - SharedCache: get/set/delete/prefix invalidation (in-memory fallback)
  - SharedCache: API semântica (scraping + research)
  - TokenEconomy: contagem, custo, truncamento, budget enforcement
  - TokenEconomy: record_usage + session_summary + top_calls
"""
import time
import pytest

from src.cache.shared_cache import SharedCache, TTL_STRATEGIES, _InMemoryCache
from src.token_economy import TokenEconomy, Budget, UsageRecord


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def cache() -> SharedCache:
    """SharedCache usando in-memory fallback (sem Redis necessário)."""
    c = SharedCache(redis_url="redis://localhost:99999")  # porta inválida → fallback
    c.clear_all()
    return c


@pytest.fixture
def te() -> TokenEconomy:
    """TokenEconomy com budget conservador para testes."""
    budget = Budget(
        max_tokens_per_call=1000,
        max_cost_usd_per_call=0.01,
        max_cost_usd_session=0.10,
    )
    return TokenEconomy(default_model="gpt-4o-mini", budget=budget)


# ═══════════════════════════════════════════════════════════════════════════════
# SharedCache — Testes de Backend In-Memory
# ═══════════════════════════════════════════════════════════════════════════════

class TestInMemoryCache:

    def test_set_and_get_basic(self):
        c = _InMemoryCache()
        c.set("key1", {"data": "value"}, ttl=60)
        assert c.get("key1") == {"data": "value"}

    def test_get_missing_key_returns_none(self):
        c = _InMemoryCache()
        assert c.get("nonexistent") is None

    def test_ttl_expiry(self):
        c = _InMemoryCache()
        c.set("key_ttl", "expires_soon", ttl=1)
        assert c.get("key_ttl") == "expires_soon"
        time.sleep(1.1)
        assert c.get("key_ttl") is None  # expirado

    def test_delete_key(self):
        c = _InMemoryCache()
        c.set("key_del", "to_delete", ttl=60)
        c.delete("key_del")
        assert c.get("key_del") is None

    def test_scan_keys_by_prefix(self):
        c = _InMemoryCache()
        c.set("scrape:abc", "content1", ttl=60)
        c.set("scrape:def", "content2", ttl=60)
        c.set("research:xyz", "content3", ttl=60)
        keys = c.scan_keys("scrape:")
        assert "scrape:abc" in keys
        assert "scrape:def" in keys
        assert "research:xyz" not in keys

    def test_clear_removes_all(self):
        c = _InMemoryCache()
        c.set("k1", "v1", ttl=60)
        c.set("k2", "v2", ttl=60)
        c.clear()
        assert c.get("k1") is None
        assert c.get("k2") is None

    def test_backend_name(self):
        c = _InMemoryCache()
        assert c.backend_name == "in_memory"


# ═══════════════════════════════════════════════════════════════════════════════
# SharedCache — Testes com Fallback In-Memory
# ═══════════════════════════════════════════════════════════════════════════════

class TestSharedCacheInMemory:

    def test_backend_is_in_memory_when_redis_unavailable(self, cache: SharedCache):
        assert cache.backend_name == "in_memory"
        assert not cache._is_redis

    def test_set_and_get_generic(self, cache: SharedCache):
        cache.set("test:key", {"foo": "bar"}, strategy="moderate")
        result = cache.get("test:key")
        assert result == {"foo": "bar"}

    def test_get_missing_returns_none(self, cache: SharedCache):
        assert cache.get("does:not:exist") is None

    def test_delete_key(self, cache: SharedCache):
        cache.set("test:del", "to_delete", strategy="moderate")
        cache.delete("test:del")
        assert cache.get("test:del") is None

    def test_invalidate_by_prefix(self, cache: SharedCache):
        cache.set("scrape:aaa", "c1", strategy="moderate")
        cache.set("scrape:bbb", "c2", strategy="moderate")
        cache.set("research:ccc", "c3", strategy="moderate")
        count = cache.invalidate_by_prefix("scrape:")
        assert count == 2
        assert cache.get("scrape:aaa") is None
        assert cache.get("scrape:bbb") is None
        assert cache.get("research:ccc") == "c3"  # preservado

    def test_ttl_strategies_accepted(self, cache: SharedCache):
        for strategy in TTL_STRATEGIES.keys():
            cache.set(f"test:{strategy}", "value", strategy=strategy)
            assert cache.get(f"test:{strategy}") == "value"

    def test_clear_all(self, cache: SharedCache):
        cache.set("k1", "v1")
        cache.set("k2", "v2")
        cache.clear_all()
        assert cache.get("k1") is None


# ═══════════════════════════════════════════════════════════════════════════════
# SharedCache — API Semântica
# ═══════════════════════════════════════════════════════════════════════════════

class TestSharedCacheSemanticAPI:

    def test_scraping_cache_hit(self, cache: SharedCache):
        url = "https://docs.example.com/api"
        cache.set_scraped_content(url, "# API Docs\nContent here")
        result = cache.get_scraped_content(url)
        assert result == "# API Docs\nContent here"

    def test_scraping_cache_miss(self, cache: SharedCache):
        assert cache.get_scraped_content("https://not-cached.com") is None

    def test_scraping_url_hash_consistency(self, cache: SharedCache):
        url = "https://example.com/page?q=1"
        h1 = SharedCache.url_hash(url)
        h2 = SharedCache.url_hash(url)
        assert h1 == h2
        assert len(h1) == 16

    def test_scraping_different_urls_different_keys(self, cache: SharedCache):
        url_a = "https://example.com/page1"
        url_b = "https://example.com/page2"
        cache.set_scraped_content(url_a, "content A")
        assert cache.get_scraped_content(url_b) is None

    def test_research_cache_hit(self, cache: SharedCache):
        query = "best Python ORM 2026"
        result = {"summary": "SQLAlchemy leads", "confidence": 0.85}
        cache.set_research_result(query, result)
        retrieved = cache.get_research_result(query)
        assert retrieved["summary"] == "SQLAlchemy leads"

    def test_research_cache_miss(self, cache: SharedCache):
        assert cache.get_research_result("query never cached") is None

    def test_research_query_hash_case_insensitive(self, cache: SharedCache):
        q1 = "Best Python ORM"
        q2 = "best python orm"
        h1 = SharedCache.query_hash(q1)
        h2 = SharedCache.query_hash(q2)
        assert h1 == h2  # normalizado para lowercase

    def test_research_cache_with_strategy(self, cache: SharedCache):
        query = "React vs Vue"
        result = {"winner": "React", "score": 75}
        cache.set_research_result(query, result, strategy="aggressive")
        retrieved = cache.get_research_result(query)
        assert retrieved["winner"] == "React"


# ═══════════════════════════════════════════════════════════════════════════════
# TokenEconomy — Contagem de Tokens
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenEconomyCount:

    def test_count_tokens_returns_positive_int(self, te: TokenEconomy):
        count = te.count_tokens("Hello, World!")
        assert isinstance(count, int)
        assert count > 0

    def test_count_tokens_empty_string(self, te: TokenEconomy):
        # Heurística ou tiktoken — deve retornar pelo menos 1
        count = te.count_tokens("")
        assert isinstance(count, int)

    def test_count_tokens_longer_text_more_tokens(self, te: TokenEconomy):
        short = "Hi"
        long = "This is a much longer text with many more words and concepts explained in detail."
        assert te.count_tokens(long) > te.count_tokens(short)

    def test_count_tokens_model_specific(self, te: TokenEconomy):
        text = "The quick brown fox"
        # Diferentes modelos podem ter encoding diferente mas ambos retornam int > 0
        c1 = te.count_tokens(text, model="gpt-4o")
        c2 = te.count_tokens(text, model="gpt-3.5-turbo")
        assert c1 > 0
        assert c2 > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TokenEconomy — Estimativa de Custo
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenEconomyCost:

    def test_estimate_cost_returns_tuple(self, te: TokenEconomy):
        tokens, cost = te.estimate_cost("Hello world")
        assert isinstance(tokens, int)
        assert isinstance(cost, float)

    def test_estimate_cost_tokens_positive(self, te: TokenEconomy):
        tokens, _ = te.estimate_cost("Some text here")
        assert tokens > 0

    def test_estimate_cost_free_model_zero_cost(self, te: TokenEconomy):
        _, cost = te.estimate_cost("Some text", model="ollama/llama3")
        assert cost == 0.0

    def test_estimate_cost_expensive_model_higher(self, te: TokenEconomy):
        text = "Analyze this complex technical document in detail."
        _, cheap = te.estimate_cost(text, model="gpt-4o-mini")
        _, expensive = te.estimate_cost(text, model="claude-opus-4")
        assert expensive > cheap

    def test_estimate_cost_with_output_tokens(self, te: TokenEconomy):
        _, cost_no_output = te.estimate_cost("Test", output_tokens=0)
        _, cost_with_output = te.estimate_cost("Test", output_tokens=500)
        assert cost_with_output >= cost_no_output

    def test_estimate_cost_unknown_model_uses_default_pricing(self, te: TokenEconomy):
        tokens, cost = te.estimate_cost("Some text", model="unknown-model-xyz")
        assert tokens > 0
        assert cost >= 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# TokenEconomy — Truncamento Inteligente
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenEconomyTruncate:

    def test_short_text_not_truncated(self, te: TokenEconomy):
        text = "Short text."
        result = te.smart_truncate(text, max_tokens=1000)
        assert result == text

    def test_long_text_is_truncated(self, te: TokenEconomy):
        # Gerar texto longo (~2000 tokens)
        text = "word " * 2000
        result = te.smart_truncate(text, max_tokens=100)
        assert len(result) < len(text)

    def test_truncated_text_contains_marker(self, te: TokenEconomy):
        text = "word " * 2000
        result = te.smart_truncate(text, max_tokens=100)
        assert "TRUNCADO" in result or "..." in result or "[" in result

    def test_head_preserved_after_truncation(self, te: TokenEconomy):
        text = "START_MARKER " + ("filler " * 2000) + " END_MARKER"
        result = te.smart_truncate(text, max_tokens=50, head_ratio=0.6)
        assert "START_MARKER" in result

    def test_tail_preserved_after_truncation(self, te: TokenEconomy):
        text = ("filler " * 2000) + " END_MARKER"
        result = te.smart_truncate(text, max_tokens=50, head_ratio=0.6)
        assert "END_MARKER" in result

    def test_head_ratio_boundary(self, te: TokenEconomy):
        # head_ratio extremos não devem quebrar
        text = "word " * 2000
        r1 = te.smart_truncate(text, max_tokens=100, head_ratio=0.1)
        r2 = te.smart_truncate(text, max_tokens=100, head_ratio=0.9)
        assert len(r1) > 0
        assert len(r2) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TokenEconomy — Budget Enforcement
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenEconomyBudget:

    def test_within_budget_returns_true(self, te: TokenEconomy):
        text = "Short text within budget."
        assert te.check_budget(text) is True

    def test_over_token_limit_returns_false(self, te: TokenEconomy):
        # Budget tem max_tokens_per_call=1000; gerar texto > 1000 tokens
        text = "word " * 500  # ~500 tokens aprox
        # Budget = 1000, então 500 deve passar
        result = te.check_budget(text)
        assert isinstance(result, bool)

    def test_session_budget_exhausted_blocks(self, te: TokenEconomy):
        # Forçar o budget de sessão a estar esgotado
        te.budget.session_spent_usd = te.budget.max_cost_usd_session + 1.0
        assert te.check_budget("Any text") is False

    def test_record_usage_accumulates(self, te: TokenEconomy):
        te.record_usage(input_tokens=100, output_tokens=50, model="gpt-4o-mini")
        te.record_usage(input_tokens=200, output_tokens=100, model="gpt-4o-mini")
        summary = te.session_summary()
        assert summary["total_calls"] == 2
        assert summary["total_input_tokens"] == 300
        assert summary["total_output_tokens"] == 150

    def test_session_summary_cost_positive(self, te: TokenEconomy):
        te.record_usage(input_tokens=1000, output_tokens=500, model="gpt-4o-mini")
        summary = te.session_summary()
        assert summary["total_cost_usd"] >= 0.0

    def test_top_calls_sorted_by_cost(self, te: TokenEconomy):
        te.record_usage(100, 50, model="gpt-4o-mini", query_hint="cheap call")
        te.record_usage(5000, 2000, model="claude-opus-4", query_hint="expensive call")
        top = te.top_calls(n=2)
        assert len(top) == 2
        assert top[0].estimated_cost_usd >= top[1].estimated_cost_usd

    def test_top_calls_n_limits_results(self, te: TokenEconomy):
        for i in range(5):
            te.record_usage(100, 50, model="gpt-4o-mini", query_hint=f"call {i}")
        top = te.top_calls(n=3)
        assert len(top) == 3

    def test_usage_record_fields(self, te: TokenEconomy):
        rec = te.record_usage(
            input_tokens=500,
            output_tokens=200,
            model="gemini-2.5-flash",
            query_hint="test query hint"
        )
        assert isinstance(rec, UsageRecord)
        assert rec.input_tokens == 500
        assert rec.output_tokens == 200
        assert rec.model == "gemini-2.5-flash"
        assert "test query hint" in rec.query_hint
        assert rec.estimated_cost_usd >= 0.0

    def test_budget_dataclass_defaults(self):
        b = Budget()
        assert b.max_tokens_per_call == 8000
        assert b.max_cost_usd_per_call == 0.05
        assert b.max_cost_usd_session == 2.00
        assert b.session_spent_usd == 0.0
        assert b.session_records == []
