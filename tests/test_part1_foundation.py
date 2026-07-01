import pytest
import tempfile
import os
from pathlib import Path


def test_types_import():
    from src.types import (
        Domain, Intention, IntentResult, ExpandedQuery, SearchResult,
        RankedResult, SourcePlan, GapAnalysis, SynthesizedResult, ResearchMetadata,
    )
    assert Domain.SAAS_B2B == "saas_b2b"
    assert Intention.DISCOVER == "discover"


def test_config_import():
    from src.config import Config, LLMProvider
    config = Config(_env_file=None)
    assert config.llm_provider == LLMProvider.ANTHROPIC
    assert config.max_results_per_source == 20


def test_config_get_llm_config_anthropic():
    from src.config import Config, LLMProvider
    config = Config(llm_provider=LLMProvider.ANTHROPIC, anthropic_api_key="test-key")
    llm_cfg = config.get_llm_config()
    assert llm_cfg["api_key"] == "test-key"
    assert "claude" in llm_cfg["model"]


def test_config_get_llm_config_openai():
    from src.config import Config, LLMProvider
    config = Config(llm_provider=LLMProvider.OPENAI, openai_api_key="test-key")
    llm_cfg = config.get_llm_config()
    assert llm_cfg["api_key"] == "test-key"


def test_config_get_llm_config_ollama():
    from src.config import Config, LLMProvider
    config = Config(llm_provider=LLMProvider.OLLAMA)
    llm_cfg = config.get_llm_config()
    assert "base_url" in llm_cfg
    assert "model" in llm_cfg


def test_http_client_init():
    from src.utils.http_client import HTTPClient
    client = HTTPClient(timeout=10, max_retries=2)
    assert client.max_retries == 2


def test_query_cleaner_clean():
    from src.utils.query_cleaner import QueryCleaner
    result = QueryCleaner.clean("What is the best CRM?")
    assert "what" not in result
    assert "best" not in result
    assert "crm" in result


def test_query_cleaner_disambiguate():
    from src.utils.query_cleaner import QueryCleaner
    results = QueryCleaner.disambiguate("java programming")
    assert len(results) > 0
    assert any("java" in r for r in results)


def test_cache_set_get():
    from src.utils.cache import Cache
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, ttl_hours=1)
        cache.set("test", "my_query", {"data": "value"})
        result = cache.get("test", "my_query")
        assert result == {"data": "value"}


def test_cache_miss():
    from src.utils.cache import Cache
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir)
        result = cache.get("test", "nonexistent_query")
        assert result is None


def test_cache_invalidate():
    from src.utils.cache import Cache
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir)
        cache.set("prefix", "q1", "val1")
        cache.set("prefix", "q2", "val2")
        cache.invalidate("prefix")
        assert cache.get("prefix", "q1") is None
        assert cache.get("prefix", "q2") is None


def test_deduplicator_by_url(sample_search_result):
    from src.utils.deduplicator import Deduplicator
    from src.types import SearchResult
    r1 = sample_search_result
    r2 = SearchResult(
        source="hackernews",
        title="Twenty CRM",
        url="https://github.com/twentyhq/twenty",
        description="Same URL different title",
    )
    assert Deduplicator.is_duplicate(r1, r2)


def test_deduplicator_by_title_similarity(sample_search_result):
    from src.utils.deduplicator import Deduplicator
    from src.types import SearchResult
    r1 = sample_search_result
    r2 = SearchResult(
        source="reddit",
        title="twenty/twenty crm open source",
        url="https://reddit.com/different",
        description="Reddit post about twenty",
    )
    assert Deduplicator.is_duplicate(r1, r2)


def test_deduplicator_unique_results(sample_search_result):
    from src.utils.deduplicator import Deduplicator
    from src.types import SearchResult
    r1 = sample_search_result
    r2 = SearchResult(
        source="github",
        title="supabase/supabase",
        url="https://github.com/supabase/supabase",
        description="Open source Firebase alternative",
    )
    results = Deduplicator.deduplicate([r1, r2, r1])
    assert len(results) == 2
