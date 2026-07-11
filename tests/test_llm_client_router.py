"""Tests for LLMClient integration with the active SmartModelRouter.

NOTE: the legacy `src.model_router.ModelRouter` was removed in Fase 6.
Model routing now lives in `src.clients.smart_model_router.SmartModelRouter`,
which returns a `RoutingDecision(tier, model_id, score, reason)`.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.clients.llm_client import LLMClient, LLMProvider
from src.clients.smart_model_router import SmartModelRouter, ModelTier


def _make_client(
    provider: LLMProvider = LLMProvider.ANTHROPIC, router=None
) -> LLMClient:
    with patch("anthropic.AsyncAnthropic"):
        client = LLMClient(
            provider,
            {"api_key": "test-key", "model": "claude-sonnet-4-6"},
            model_router=router,
        )
    return client


# ─── __init__: model_router parameter ───────────────────────────────────────


def test_llm_client_accepts_model_router():
    router = SmartModelRouter()
    client = _make_client(router=router)
    assert client.model_router is router


def test_llm_client_default_no_router():
    client = _make_client()
    assert client.model_router is None


def test_llm_client_existing_tests_still_pass():
    """Confirms backward compatibility: init without router works identically."""
    with patch("anthropic.AsyncAnthropic"):
        client = LLMClient(
            LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"}
        )
    assert client.model == "claude-test"
    assert client.provider == LLMProvider.ANTHROPIC
    assert client.model_router is None


# ─── complete(): no router → delegates to generate ──────────────────────────


@pytest.mark.asyncio
async def test_complete_no_router_calls_generate(caplog):
    client = _make_client()
    client.generate = AsyncMock(return_value="generated text")

    result = await client.complete("test prompt")

    assert result == "generated text"
    client.generate.assert_called_once_with(
        "test prompt", temperature=0.3, max_tokens=4000
    )


@pytest.mark.asyncio
async def test_complete_no_router_passes_temperature_and_max_tokens():
    client = _make_client()
    client.generate = AsyncMock(return_value="ok")

    await client.complete("prompt", temperature=0.7, max_tokens=1000)

    client.generate.assert_called_once_with("prompt", temperature=0.7, max_tokens=1000)


# ─── complete(): with router → selects model automatically ──────────────────


@pytest.mark.asyncio
async def test_complete_with_router_routes_simple_task():
    router = SmartModelRouter()
    client = _make_client(router=router)
    client.generate = AsyncMock(return_value="answer")

    await client.complete("classify this", task_type="intent")

    # model is restored to the configured default after the call
    assert client.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_complete_with_router_uses_routed_model_during_call():
    """The model is temporarily switched to the routed model during generate()."""
    router = SmartModelRouter()
    client = _make_client(router=router)

    used_models = []

    async def capture_model(prompt, temperature, max_tokens):
        used_models.append(client.model)
        return "ok"

    client.generate = capture_model

    await client.complete("prompt", task_type="intent")

    # intent -> free tier (haiku) unless groq-free override kicks in
    assert used_models[0] in (
        "claude-haiku-4-5-20251001",
        "meta-llama/llama-3.1-8b-instruct:free",
    )


@pytest.mark.asyncio
async def test_complete_with_router_restores_original_model_after_call():
    router = SmartModelRouter()
    client = _make_client(router=router)
    original = client.model
    client.generate = AsyncMock(return_value="ok")

    await client.complete("prompt", task_type="deep_research")

    assert client.model == original


@pytest.mark.asyncio
async def test_complete_with_router_restores_model_even_on_error():
    router = SmartModelRouter()
    client = _make_client(router=router)
    original = client.model
    client.generate = AsyncMock(side_effect=RuntimeError("API error"))

    with pytest.raises(RuntimeError):
        await client.complete("prompt", task_type="deep_research")

    assert client.model == original


# ─── complete(): model_override bypasses router ──────────────────────────────


@pytest.mark.asyncio
async def test_complete_model_override_ignores_router():
    router = SmartModelRouter()
    client = _make_client(router=router)

    used_models = []

    async def capture(prompt, temperature, max_tokens):
        used_models.append(client.model)
        return "ok"

    client.generate = capture

    await client.complete(
        "prompt", task_type="intent", model_override="claude-opus-4-8"
    )

    assert used_models[0] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_complete_model_override_restores_original_model():
    client = _make_client()
    original = client.model
    client.generate = AsyncMock(return_value="ok")

    await client.complete("prompt", model_override="gpt-4o")

    assert client.model == original


@pytest.mark.asyncio
async def test_complete_model_override_no_router_still_works():
    client = _make_client()
    used_models = []

    async def capture(prompt, temperature, max_tokens):
        used_models.append(client.model)
        return "ok"

    client.generate = capture

    await client.complete("prompt", model_override="gemini-2.5-pro")

    assert used_models[0] == "gemini-2.5-pro"


# ─── complete(): different task_type tiers ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type,expected_tier",
    [
        ("intent", "free"),
        ("expand", "haiku"),
        ("synthesis", "sonnet"),
        ("report", "sonnet"),
        ("deep", "opus"),
    ],
)
async def test_complete_routes_all_task_types(task_type, expected_tier):
    router = SmartModelRouter()
    client = _make_client(router=router)
    used_models = []

    async def capture(prompt, temperature, max_tokens):
        used_models.append(client.model)
        return "ok"

    client.generate = capture

    await client.complete("test", task_type=task_type)

    # The routed model_id corresponds to the expected tier.
    decision = router.route(task_type, "anthropic")
    assert decision.tier == expected_tier
    assert used_models[0] == decision.model_id


# ─── backward compat: generate() signature unchanged ────────────────────────


@pytest.mark.asyncio
async def test_generate_signature_unchanged():
    """generate() still works exactly as before — no regression."""
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="response")])
        )
        MockAnthropic.return_value = mock_instance

        client = LLMClient(
            LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"}
        )
        result = await client.generate("test prompt")

    assert result == "response"
