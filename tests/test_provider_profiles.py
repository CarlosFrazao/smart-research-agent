"""
Testes do ProviderRegistry (FEAT-007) e do wire no SmartModelRouter.

Cobre (PRD 4.7.2 / 4.7.3):
  - carrega YAML → 3 providers roteáveis por tier
  - YAML ausente → registry vazio (fallback hardcoded preservado)
  - YAML inválido (schema) → provider inválido ignorado, fallback
  - register_provider dinâmico no SmartModelRouter
  - route() respeita model_id do registry quando carregado
"""

from __future__ import annotations

import textwrap

from src.clients.provider_profiles import ProviderProfile, ProviderRegistry
from src.clients.smart_model_router import SmartModelRouter


def _write_yaml(tmp_path, content: str) -> str:
    p = tmp_path / "providers.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(p)


def test_load_yaml_three_providers(tmp_path):
    yaml_path = _write_yaml(
        tmp_path,
        """
        providers:
          - name: anthropic
            api_key_env: ANTHROPIC_API_KEY
            models:
              free: claude-haiku-4-5-20251001
              haiku: claude-haiku-4-5-20251001
              sonnet: claude-sonnet-4-6
              opus: claude-opus-4-8
          - name: gemini
            api_key_env: GEMINI_API_KEY
            models:
              free: gemini-2.5-flash
              haiku: gemini-2.5-flash
              sonnet: gemini-2.5-flash
              opus: gemini-2.5-pro
          - name: openrouter
            api_key_env: OPENROUTER_API_KEY
            models:
              free: meta-llama/llama-3.1-8b-instruct:free
              haiku: anthropic/claude-haiku-4-5
              sonnet: anthropic/claude-sonnet-4-6
              opus: anthropic/claude-opus-4-8
        """,
    )
    registry = ProviderRegistry.load(yaml_path)
    assert len(registry) == 3
    assert set(registry.names()) == {"anthropic", "gemini", "openrouter"}
    # Tier sonnet do anthropic roteia para claude-sonnet-4-6
    assert registry.get("anthropic").get_model("sonnet") == "claude-sonnet-4-6"
    # Tier opus do gemini
    assert registry.get("gemini").get_model("opus") == "gemini-2.5-pro"
    # Tier inexistente → None
    assert registry.get("anthropic").get_model("nope") is None


def test_missing_yaml_falls_back_empty(tmp_path):
    missing = str(tmp_path / "does_not_exist.yaml")
    registry = ProviderRegistry.load(missing)
    assert len(registry) == 0
    assert registry.get("anthropic") is None


def test_invalid_provider_skipped(tmp_path):
    yaml_path = _write_yaml(
        tmp_path,
        """
        providers:
          - name: broken
            # sem 'models' → ValidationError → ignorado
            api_key_env: X
          - name: ok
            api_key_env: OK_KEY
            models:
              free: m-free
              haiku: m-haiku
              sonnet: m-sonnet
              opus: m-opus
        """,
    )
    registry = ProviderRegistry.load(yaml_path)
    assert len(registry) == 1
    assert registry.get("ok") is not None
    assert registry.get("broken") is None


def test_duplicate_provider_last_wins(tmp_path):
    yaml_path = _write_yaml(
        tmp_path,
        """
        providers:
          - name: p
            api_key_env: K1
            models: {free: a, haiku: a, sonnet: a, opus: a}
          - name: p
            api_key_env: K2
            models: {free: b, haiku: b, sonnet: b, opus: b}
        """,
    )
    registry = ProviderRegistry.load(yaml_path)
    assert len(registry) == 1
    assert registry.get("p").get_model("free") == "b"


def test_router_uses_registry_when_loaded(tmp_path):
    yaml_path = _write_yaml(
        tmp_path,
        """
        providers:
          - name: anthropic
            api_key_env: ANTHROPIC_API_KEY
            models:
              free: claude-haiku-4-5-20251001
              haiku: claude-haiku-4-5-20251001
              sonnet: claude-sonnet-4-6-CUSTOM
              opus: claude-opus-4-8
        """,
    )
    router = SmartModelRouter()
    assert router.load_provider_profiles(yaml_path) is True
    decision = router.route("synthesis", provider="anthropic")
    assert decision.model_id == "claude-sonnet-4-6-CUSTOM"
    # Tier opus também vem do registry
    deep = router.route("deep", provider="anthropic")
    assert deep.model_id == "claude-opus-4-8"


def test_router_fallback_when_yaml_missing(tmp_path):
    router = SmartModelRouter()
    # Carrega arquivo inexistente → fallback hardcoded mantido.
    assert router.load_provider_profiles(str(tmp_path / "nope.yaml")) is False
    decision = router.route("synthesis", provider="anthropic")
    # Tier sonnet hardcoded do anthropic: claude-sonnet-4-6
    assert decision.model_id == "claude-sonnet-4-6"


def test_register_provider_dynamic(tmp_path):
    router = SmartModelRouter()
    router.register_provider(
        ProviderProfile(
            name="custom",
            api_key_env="CUSTOM_ENV_VAR",  # pragma: allowlist secret
            models={
                "free": "c-free",
                "haiku": "c-haiku",
                "sonnet": "c-sonnet",
                "opus": "c-opus",
            },
        )
    )
    decision = router.route("report", provider="custom")
    assert decision.model_id == "c-sonnet"
