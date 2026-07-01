"""
ModelRouter — Smart Model Routing by Task Complexity

Inspired by: Evo-Nexus multi-provider architecture

Philosophy: use the CHEAPEST model that achieves the required quality.
This reduces cost 60-80% without sacrificing perceived quality.
"""
import logging
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)

_SIMPLE_TASKS = frozenset({
    "intent_analysis",
    "deduplication",
    "query_cleaning",
    "relevance_check",
})

_MEDIUM_TASKS = frozenset({
    "query_expansion",
    "ranking",
    "gap_detection",
    "synthesis",
})

_COMPLEX_TASKS = frozenset({
    "report_generation",
    "deep_research",
    "confidence_scoring",
})

_REASONING_TASKS = frozenset({
    "deep_research",
    "confidence_scoring",
    "research_auditing",
})

_PRICES_PER_1K_TOKENS: Dict[str, float] = {
    "claude-haiku-4-5": 0.001,
    "claude-sonnet-4-5": 0.015,
    "claude-opus-4-5": 0.075,
    "gpt-4o-mini": 0.0015,
    "gpt-4o": 0.030,
    "o3-mini": 0.011,
    "deepseek-r1": 0.0014,
    "gemini-2.0-flash": 0.0003,
    "gemini-2.5-pro": 0.007,
    "gemini-2.5-flash-thinking": 0.001,
}

_ROUTING_TABLE: Dict[str, Dict[str, str]] = {
    "simple": {
        "anthropic": "claude-haiku-4-5",
        "openai": "gpt-4o-mini",
        "google": "gemini-2.0-flash",
        "openrouter": "anthropic/claude-haiku-4-5",
        "ollama": "llama3.1",
    },
    "medium": {
        "anthropic": "claude-sonnet-4-5",
        "openai": "gpt-4o",
        "google": "gemini-2.5-pro",
        "openrouter": "anthropic/claude-sonnet-4-5",
        "ollama": "llama3.1",
    },
    "complex": {
        "anthropic": "claude-opus-4-5",
        "openai": "gpt-4o",
        "google": "gemini-2.5-pro",
        "openrouter": "anthropic/claude-opus-4-5",
        "ollama": "llama3.1",
    },
    "reasoning": {
        "anthropic":  "claude-opus-4-5",
        "openai":     "o3-mini",
        "google":     "gemini-2.5-flash-thinking",
        "openrouter": "deepseek/deepseek-r1",
        "deepseek":   "deepseek-r1",
        "ollama":     "deepseek-r1:1.5b",
    },
}


@dataclass
class TaskComplexity:
    """Classification result for a given task type."""
    level: str            # "simple" | "medium" | "complex"
    reasoning: str
    estimated_tokens: int


class ModelRouter:
    """
    Routes LLM tasks to the most cost-effective model for the required quality.

    Simple tasks  → lightweight models  (Haiku, GPT-4o-mini, Gemini Flash)
    Medium tasks  → mid-tier models     (Sonnet, GPT-4o, Gemini Pro)
    Complex tasks → powerful models     (Opus, GPT-4o, Gemini Pro)
    Reasoning     → reasoning models    (o3-mini, DeepSeek-R1, Gemini Thinking)
    """

    def __init__(self, config=None):
        if config is None:
            try:
                from src.config import Config
                self.config = Config()
            except Exception:
                self.config = None
        else:
            self.config = config

    def route(self, task_type: str, provider: str) -> str:
        """
        Returns the model_id for the given task_type and provider.

        Falls back to the medium-tier model when the provider is unknown or
        when the task_type is not in any known category.
        """
        complexity = self._classify_task(task_type)
        level = complexity.level
        
        reasoning_enabled = getattr(self.config, "reasoning_models_enabled", False) if self.config else False
        if reasoning_enabled and task_type in _REASONING_TASKS:
            level = "reasoning"

        tier = _ROUTING_TABLE.get(level, _ROUTING_TABLE["medium"])
        model = tier.get(provider, tier.get("anthropic", "claude-sonnet-4-5"))
        
        # Override dinâmico vindo das configurações se disponível
        if self.config and level == "reasoning":
            if provider == "deepseek" and getattr(self.config, "deepseek_model", None):
                model = self.config.deepseek_model
            elif provider == "openai" and getattr(self.config, "openai_reasoning_model", None):
                model = self.config.openai_reasoning_model

        logger.debug(
            f"ModelRouter: task={task_type} provider={provider} "
            f"complexity={complexity.level} (routed_level={level}) → model={model}"
        )
        return model

    def _classify_task(self, task_type: str) -> TaskComplexity:
        """Classifies a task_type into simple / medium / complex."""
        if task_type in _SIMPLE_TASKS:
            return TaskComplexity(
                level="simple",
                reasoning=f"{task_type} requires straightforward classification logic",
                estimated_tokens=1_000,
            )
        if task_type in _MEDIUM_TASKS:
            return TaskComplexity(
                level="medium",
                reasoning=f"{task_type} requires moderate analysis and structured output",
                estimated_tokens=5_000,
            )
        return TaskComplexity(
            level="complex",
            reasoning=f"{task_type} requires deep reasoning or long-form generation",
            estimated_tokens=20_000,
        )

    def log_cost(self, task_type: str, tokens_used: int, model_id: str) -> None:
        """Logs the estimated cost for a completed task."""
        price = _PRICES_PER_1K_TOKENS.get(model_id, 0.015)
        cost = (tokens_used / 1_000) * price
        logger.info(
            f"COST | task={task_type} | model={model_id} "
            f"| tokens={tokens_used} | cost=${cost:.4f}"
        )

    def get_complexity(self, task_type: str) -> str:
        """Returns just the complexity level string for a task_type."""
        return self._classify_task(task_type).level
