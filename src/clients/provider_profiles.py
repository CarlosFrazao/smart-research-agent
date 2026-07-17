"""
ProviderRegistry — registry declarativo de providers para o SmartModelRouter.

Portado do padrão ``providers/base.py`` + ``providers/__init__.py`` do
Hermes Agent (Nous Research, MIT, somente-leitura), adaptado para o SRA:

- Remove qualquer acoplamento a OpenRouter/Nous do Hermes.
- Os providers são DECLARATIVOS: descrevem os model_ids por tier e o nome
  da env var da API key. O transporte de fato fica no ``LLMClient``.
- Sem segredos no YAML: só ``api_key_env`` (nome da variável de ambiente).
- ``redact_sensitive_text`` é aplicado em qualquer log que possa conter
  configuração sensível.

Uso::

    from src.clients.provider_profiles import ProviderRegistry

    registry = ProviderRegistry.load("config/providers.yaml")
    profile = registry.get("anthropic")   # ProviderProfile ou None
    for p in registry.list_providers():
        ...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from src.logging_utils import redact_sensitive_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderProfile:
    """Perfil declarativo de um provider de inferência.

    Descreve apenas o que o roteador precisa saber: model_ids por tier e
    como obter a API key (via ``api_key_env``). Não constrói clientes.
    """

    name: str
    display_name: str = ""
    api_key_env: str = ""
    base_url: str = ""
    models: dict[str, str] = field(default_factory=dict)

    def get_model(self, tier: str) -> str | None:
        """Retorna o model_id para o tier, ou None se o tier não existir."""
        return self.models.get(tier)

    def has_api_key(self) -> bool:
        """True se a env var da API key está presente e não vazia."""
        if not self.api_key_env:
            return False
        return bool(os.environ.get(self.api_key_env, "").strip())


class _ProviderModel(BaseModel):
    """Schema de validação (pydantic) de um provider no YAML."""

    name: str
    display_name: str = ""
    api_key_env: str = ""
    base_url: str = ""
    models: dict[str, str]

    @field_validator("models")
    @classmethod
    def _non_empty_models(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("'models' não pode ser vazio")
        return v


class ProviderRegistry:
    """Registry de ``ProviderProfile`` carregado de um YAML declarativo."""

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfile] = {}

    # ── Construção ────────────────────────────────────────────────

    @classmethod
    def load(cls, yaml_path: str) -> "ProviderRegistry":
        """Carrega providers de um YAML.

        O YAML deve ter a forma::

            providers:
              - name: anthropic
                api_key_env: ANTHROPIC_API_KEY
                models:
                  free: claude-haiku-4-5-20251001
                  haiku: claude-haiku-4-5-20251001
                  sonnet: claude-sonnet-4-6
                  opus: claude-opus-4-8

        Em caso de YAML ausente/inválido, retorna um registry VAZIO — o
        chamador deve usar os tiers hardcoded do ``SmartModelRouter`` como
        fallback. Nunca levanta para ausência de arquivo.
        """
        registry = cls()
        if not os.path.exists(yaml_path):
            logger.warning(
                "ProviderRegistry: arquivo %s ausente — usando fallback hardcoded.",
                redact_sensitive_text(yaml_path),
            )
            return registry
        try:
            with open(yaml_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "ProviderRegistry: falha ao ler %s (%s) — fallback hardcoded.",
                redact_sensitive_text(yaml_path),
                redact_sensitive_text(str(exc)),
            )
            return registry

        if not isinstance(data, dict):
            logger.warning(
                "ProviderRegistry: %s não é um mapping YAML — fallback hardcoded.",
                redact_sensitive_text(yaml_path),
            )
            return registry

        raw_list = data.get("providers")
        if not isinstance(raw_list, list):
            logger.warning(
                "ProviderRegistry: 'providers' ausente/inválido em %s — fallback.",
                redact_sensitive_text(yaml_path),
            )
            return registry

        seen: set[str] = set()
        for entry in raw_list:
            try:
                validated = _ProviderModel.model_validate(entry)
            except ValidationError as exc:
                logger.warning(
                    "ProviderRegistry: provider ignorado (%s) — fallback hardcoded.",
                    redact_sensitive_text(str(exc)),
                )
                continue
            # Provider duplicado → último wins + warn (PRD 4.7.3).
            if validated.name in seen:
                logger.warning(
                    "ProviderRegistry: provider duplicado '%s' — último wins.",
                    validated.name,
                )
            seen.add(validated.name)
            registry._profiles[validated.name] = ProviderProfile(
                name=validated.name,
                display_name=validated.display_name or validated.name,
                api_key_env=validated.api_key_env,
                base_url=validated.base_url,
                models=dict(validated.models),
            )
        return registry

    # ── API ──────────────────────────────────────────────────────

    def register_provider(self, profile: ProviderProfile) -> None:
        """Registra (ou sobrescreve por name) um provider dinamicamente.

        Permite adicionar providers sem editar o core (PRD 4.7.2).
        """
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ProviderProfile | None:
        """Retorna o perfil pelo nome, ou None."""
        return self._profiles.get(name)

    def list_providers(self) -> list[ProviderProfile]:
        """Retorna todos os perfis registrados (uma lista estável)."""
        return list(self._profiles.values())

    def names(self) -> list[str]:
        """Retorna os nomes de todos os providers registrados."""
        return list(self._profiles.keys())

    def __len__(self) -> int:
        return len(self._profiles)
