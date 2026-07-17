"""
SmartModelRouter — roteamento de tarefas por complexidade para o smart-research-agent.

Adaptado do model_router.py do ORVIX-AI para o contexto de pesquisa:

Tiers de roteamento (provider Anthropic):
  free   (score <= 2): Groq llama-3.1-8b-instruct via OpenRouter (gratuito)
                       Fallback: claude-haiku-4-5-20251001
  haiku  (score 3-4):  claude-haiku-4-5-20251001  — barato para tarefas médias
  sonnet (score 5-6):  claude-sonnet-4-6           — síntese e geração de relatório
  opus   (score >= 7): claude-opus-4-8             — deep research, análise de confiança

Tarefas mapeadas por task_type:
  "intent"      → score 2  (free: classificar domínio e intenção)
  "gap"         → score 2  (free: detectar gaps simples)
  "expand"      → score 3  (haiku: expandir queries)
  "rank"        → score 4  (haiku: ranquear resultados)
  "confidence"  → score 5  (sonnet: scoring de confiança)
  "synthesis"   → score 6  (sonnet: síntese de resultados)
  "report"      → score 6  (sonnet: geração de relatório)
  "deep"        → score 8  (opus: deep research)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.clients.provider_profiles import ProviderRegistry

logger = logging.getLogger(__name__)

ModelTier = Literal["free", "haiku", "sonnet", "opus"]

# IDs de modelo por tier (provider Anthropic)
_ANTHROPIC_MODELS: dict[ModelTier, str] = {
    "free": "claude-haiku-4-5-20251001",  # fallback se Groq indisponível
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}

# IDs de modelo por tier (provider OpenRouter)
_OPENROUTER_MODELS: dict[ModelTier, str] = {
    "free": "meta-llama/llama-3.1-8b-instruct:free",
    "haiku": "anthropic/claude-haiku-4-5",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-8",
}

# IDs de modelo por tier (provider Gemini)
_GEMINI_MODELS: dict[ModelTier, str] = {
    "free": "gemini-2.5-flash",
    "haiku": "gemini-2.5-flash",
    "sonnet": "gemini-2.5-flash",
    "opus": "gemini-2.5-pro",
}

# IDs de modelo por tier (provider Groq)
_GROQ_MODELS: dict[ModelTier, str] = {
    "free": "llama-3.3-70b-versatile",
    "haiku": "llama-3.1-8b-instant",
    "sonnet": "llama-3.3-70b-versatile",
    "opus": "llama-3.3-70b-versatile",
}

# IDs de modelo por tier (provider Ollama)
_OLLAMA_MODELS: dict[ModelTier, str] = {
    "free": "llama3.2",
    "haiku": "llama3.2",
    "sonnet": "qwen2.5:3b",
    "opus": "qwen2.5:3b",
}


# Mapa de task_type → score de complexidade
_TASK_SCORES: dict[str, int] = {
    "intent": 2,
    "gap": 2,
    "expand": 3,
    "rank": 4,
    "confidence": 5,
    "synthesis": 6,
    "report": 6,
    "deep": 8,
}

_COMPLEXITY_RE = re.compile(
    r"\b(architect|refactor|debug|security|audit|migration|analys|"
    r"optim|scalab|perform|vulnerab|concurren|distribut|orchestrat)\w*\b",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(
    r"\b(step[- ]by[- ]step|plan|analyz|compar|evaluat|tradeoff|"
    r"pros and cons|break down|walk me through)\w*\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutingDecision:
    tier: ModelTier
    model_id: str
    score: int
    reason: str
    provider_override: str | None = None  # "openrouter" quando usar tier free via Groq


class SmartModelRouter:
    """
    Roteia tarefas para o modelo mais barato capaz de executá-las.

    Integração com LLMClient:
      - Para tarefas "free" com OPENROUTER_API_KEY disponível, retorna
        um model_id do OpenRouter e sinaliza provider_override="openrouter"
      - Para demais tiers, retorna o model_id do provider atual
    """

    def __init__(self, openrouter_api_key: str | None = None):
        if openrouter_api_key is not None:
            self._openrouter_key = openrouter_api_key
        else:
            self._openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        # Registry declarativo (FEAT-007). None até load_provider_profiles().
        self._registry: "ProviderRegistry | None" = None

    # ── Registry declarativo (FEAT-007) ──────────────────────────

    def load_provider_profiles(self, yaml_path: str) -> bool:
        """Carrega providers de um YAML declarativo (FEAT-007).

        Em caso de falha (arquivo ausente/inválido), mantém ``_registry=None``
        e o roteador continua usando os tiers hardcoded (fallback). Retorna
        True se ao menos um provider foi carregado.
        """
        from src.clients.provider_profiles import ProviderRegistry

        self._registry = ProviderRegistry.load(yaml_path)
        loaded = len(self._registry)
        if loaded == 0:
            logger.warning(
                "SmartModelRouter: nenhum provider do YAML carregado — fallback hardcoded."
            )
            return False
        logger.info("SmartModelRouter: %d provider(s) carregado(s) do YAML.", loaded)
        return True

    def register_provider(self, profile) -> None:
        """Registra um provider dinamicamente (FEAT-007).

        Permite adicionar providers sem editar o core. Cria o registry
        sob demanda se ainda não foi carregado de um YAML.
        """
        if self._registry is None:
            from src.clients.provider_profiles import ProviderRegistry

            self._registry = ProviderRegistry()
        self._registry.register_provider(profile)

    def _model_for(self, provider: str, tier: "ModelTier") -> str:
        """Resolve o model_id do tier, consultando o registry se disponível.

        Fallback: dicts hardcoded ``_ANTHROPIC_MODELS`` etc. (mantém
        backward-compat quando o YAML está ausente/inválido).
        """
        if self._registry is not None:
            profile = self._registry.get(provider)
            if profile is not None:
                model_id = profile.get_model(tier)
                if model_id:
                    return model_id
        # Fallback hardcoded.
        if provider == "openrouter":
            return _OPENROUTER_MODELS[tier]
        if provider == "gemini":
            return _GEMINI_MODELS[tier]
        if provider == "groq":
            return _GROQ_MODELS[tier]
        if provider == "ollama":
            return _OLLAMA_MODELS[tier]
        return _ANTHROPIC_MODELS[tier]

    def route(
        self,
        task_type: str,
        provider: str = "anthropic",
        query: str = "",
        context_tokens: int = 0,
    ) -> RoutingDecision:
        base_score = _TASK_SCORES.get(task_type, 5)
        dynamic_boost = self._dynamic_score(query, context_tokens)
        score = min(10, base_score + dynamic_boost)
        tier = self._tier(score)

        # Tier free: tentar OpenRouter com Llama gratuito
        if tier == "free" and self._openrouter_key:
            model_id = self._model_for("openrouter", "free")
            reason = f"task={task_type} score={score} → free tier via OpenRouter (Llama 3.1 8B)"
            logger.debug(f"SmartModelRouter: {reason}")
            return RoutingDecision(
                tier=tier,
                model_id=model_id,
                score=score,
                reason=reason,
                provider_override="openrouter",
            )

        # Demais tiers: usar modelo do provider atual (registry ou fallback)
        model_id = self._model_for(provider, tier)

        # Sobrescrita específica para o Ollama em tarefas de programação complexas
        if provider == "ollama" and tier in ("sonnet", "opus"):
            code_keywords = r"\b(code|python|java|c#|cpp|rust|html|css|js|ts|query|sql|api|rest|graphql|docker|git|develop|program|script|bug|error|exception)\w*\b"
            if re.search(code_keywords, query.lower()) or _COMPLEXITY_RE.search(query):
                model_id = "qwen2.5-coder:3b"

        reason = self._build_reason(score, task_type, query, context_tokens, tier)
        logger.debug(f"SmartModelRouter: {reason}")
        return RoutingDecision(tier=tier, model_id=model_id, score=score, reason=reason)

    def _dynamic_score(self, query: str, context_tokens: int) -> int:
        boost = 0
        if len(query) > 500:
            boost += 2
        elif len(query) > 200:
            boost += 1
        if _COMPLEXITY_RE.search(query):
            boost += 1
        if context_tokens > 8_000:
            boost += 2
        elif context_tokens > 4_000:
            boost += 1
        if _REASONING_RE.search(query):
            boost += 1
        return boost

    def _tier(self, score: int) -> ModelTier:
        if score <= 2:
            return "free"
        if score <= 4:
            return "haiku"
        if score <= 6:
            return "sonnet"
        return "opus"

    def _build_reason(
        self,
        score: int,
        task_type: str,
        query: str,
        context_tokens: int,
        tier: ModelTier,
    ) -> str:
        parts = [f"task={task_type}", f"score={score}", f"tier={tier}"]
        if len(query) > 200:
            parts.append("long-query")
        if _COMPLEXITY_RE.search(query):
            parts.append("complexity-keywords")
        if context_tokens > 4_000:
            parts.append(f"context={context_tokens}tok")
        return ", ".join(parts)


# Singleton de módulo
_router: SmartModelRouter | None = None


def get_router(openrouter_api_key: str | None = None) -> SmartModelRouter:
    global _router
    if _router is None:
        _router = SmartModelRouter(openrouter_api_key=openrouter_api_key)
    return _router
