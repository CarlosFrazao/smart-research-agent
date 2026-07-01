import pytest
import logging
from src.model_router import ModelRouter, TaskComplexity


@pytest.fixture
def router() -> ModelRouter:
    return ModelRouter()


# ─── _classify_task ──────────────────────────────────────────────────────────

def test_classify_simple_tasks(router):
    for task in ("intent_analysis", "deduplication", "query_cleaning", "relevance_check"):
        c = router._classify_task(task)
        assert c.level == "simple", f"{task} should be simple, got {c.level}"
        assert c.estimated_tokens == 1_000


def test_classify_medium_tasks(router):
    for task in ("query_expansion", "ranking", "gap_detection", "synthesis"):
        c = router._classify_task(task)
        assert c.level == "medium", f"{task} should be medium, got {c.level}"
        assert c.estimated_tokens == 5_000


def test_classify_complex_tasks(router):
    for task in ("report_generation", "deep_research", "confidence_scoring"):
        c = router._classify_task(task)
        assert c.level == "complex", f"{task} should be complex, got {c.level}"
        assert c.estimated_tokens == 20_000


def test_classify_unknown_task_defaults_complex(router):
    c = router._classify_task("some_new_unknown_task")
    assert c.level == "complex"


def test_classify_returns_task_complexity_dataclass(router):
    c = router._classify_task("intent_analysis")
    assert isinstance(c, TaskComplexity)
    assert isinstance(c.reasoning, str)
    assert len(c.reasoning) > 0


# ─── route() — Anthropic ─────────────────────────────────────────────────────

def test_route_simple_anthropic(router):
    assert router.route("intent_analysis", "anthropic") == "claude-haiku-4-5"


def test_route_medium_anthropic(router):
    assert router.route("query_expansion", "anthropic") == "claude-sonnet-4-5"


def test_route_complex_anthropic(router):
    assert router.route("deep_research", "anthropic") == "claude-opus-4-5"


def test_route_report_generation_anthropic(router):
    assert router.route("report_generation", "anthropic") == "claude-opus-4-5"


def test_route_confidence_scoring_anthropic(router):
    assert router.route("confidence_scoring", "anthropic") == "claude-opus-4-5"


# ─── route() — OpenAI ────────────────────────────────────────────────────────

def test_route_simple_openai(router):
    assert router.route("intent_analysis", "openai") == "gpt-4o-mini"


def test_route_medium_openai(router):
    assert router.route("ranking", "openai") == "gpt-4o"


def test_route_complex_openai(router):
    assert router.route("deep_research", "openai") == "gpt-4o"


# ─── route() — Google ────────────────────────────────────────────────────────

def test_route_simple_google(router):
    assert router.route("deduplication", "google") == "gemini-2.0-flash"


def test_route_medium_google(router):
    assert router.route("synthesis", "google") == "gemini-2.5-pro"


def test_route_complex_google(router):
    assert router.route("report_generation", "google") == "gemini-2.5-pro"


# ─── route() — OpenRouter ────────────────────────────────────────────────────

def test_route_simple_openrouter(router):
    assert router.route("query_cleaning", "openrouter") == "anthropic/claude-haiku-4-5"


def test_route_medium_openrouter(router):
    assert router.route("gap_detection", "openrouter") == "anthropic/claude-sonnet-4-5"


def test_route_complex_openrouter(router):
    assert router.route("deep_research", "openrouter") == "anthropic/claude-opus-4-5"


# ─── route() — Ollama ────────────────────────────────────────────────────────

def test_route_ollama_all_tiers_return_model(router):
    for task in ("intent_analysis", "query_expansion", "deep_research"):
        model = router.route(task, "ollama")
        assert isinstance(model, str)
        assert len(model) > 0


# ─── route() — unknown provider ──────────────────────────────────────────────

def test_route_unknown_provider_falls_back_to_anthropic(router):
    model = router.route("intent_analysis", "unknown_provider_xyz")
    assert model == "claude-haiku-4-5"


def test_route_unknown_task_unknown_provider_returns_string(router):
    model = router.route("made_up_task", "made_up_provider")
    assert isinstance(model, str)
    assert len(model) > 0


# ─── get_complexity() ────────────────────────────────────────────────────────

def test_get_complexity_returns_level_string(router):
    assert router.get_complexity("intent_analysis") == "simple"
    assert router.get_complexity("query_expansion") == "medium"
    assert router.get_complexity("deep_research") == "complex"


# ─── log_cost() ──────────────────────────────────────────────────────────────

def test_log_cost_known_model(router, caplog):
    with caplog.at_level(logging.INFO, logger="src.model_router"):
        router.log_cost("report_generation", 10_000, "claude-opus-4-5")
    assert "COST" in caplog.text
    assert "report_generation" in caplog.text
    assert "claude-opus-4-5" in caplog.text


def test_log_cost_unknown_model_uses_default_price(router, caplog):
    with caplog.at_level(logging.INFO, logger="src.model_router"):
        router.log_cost("synthesis", 5_000, "some-future-model")
    assert "COST" in caplog.text
    assert "some-future-model" in caplog.text


def test_log_cost_computes_nonzero(router, caplog):
    with caplog.at_level(logging.INFO, logger="src.model_router"):
        router.log_cost("deep_research", 1_000, "claude-opus-4-5")
    assert "$0." in caplog.text


def test_log_cost_zero_tokens(router, caplog):
    with caplog.at_level(logging.INFO, logger="src.model_router"):
        router.log_cost("ranking", 0, "claude-haiku-4-5")
    assert "tokens=0" in caplog.text


# ─── acceptance criteria (spec-level) ────────────────────────────────────────

def test_acceptance_intent_analysis_anthropic(router):
    """From UPGRADE_INSTRUCTIONS spec: intent_analysis/anthropic → claude-haiku-4-5"""
    assert router.route("intent_analysis", "anthropic") == "claude-haiku-4-5"


def test_acceptance_deep_research_anthropic(router):
    """From UPGRADE_INSTRUCTIONS spec: deep_research/anthropic → claude-opus-4-5"""
    assert router.route("deep_research", "anthropic") == "claude-opus-4-5"
