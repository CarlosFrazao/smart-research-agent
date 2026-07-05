"""Budget Tracker — Monitora e limita o custo financeiro das chamadas a LLMs."""

import logging

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Erro lançado quando o budget financeiro de chamadas LLM é ultrapassado."""

    pass


class BudgetTracker:
    """Rastreia e limita o custo das chamadas LLM em USD."""

    def __init__(self, max_cost_usd: float = 10.0):
        self.max_cost_usd = max_cost_usd
        self.current_cost_usd = 0.0

    def record_call(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Registra uma chamada LLM e incrementa o custo estimado.

        Lança BudgetExceededError se o custo acumulado exceder o limite.
        """
        # Estimativas simples de custos por 1M de tokens em USD
        rates = {
            "gpt-4": {"input": 30.0, "output": 60.0},
            "claude": {"input": 15.0, "output": 75.0},
            "gemini": {"input": 7.0, "output": 21.0},
        }

        # Normaliza nome do modelo
        model_key = "gemini"
        for k in rates:
            if k in model.lower():
                model_key = k
                break

        rate = rates[model_key]
        cost = (input_tokens / 1_000_000.0 * rate["input"]) + (
            output_tokens / 1_000_000.0 * rate["output"]
        )
        self.current_cost_usd += cost

        logger.debug(
            f"BudgetTracker: chamada LLM '{model}' registrada. Custo da chamada: ${cost:.6f} USD. "
            f"Custo total: ${self.current_cost_usd:.6f} USD / Max: ${self.max_cost_usd:.6f} USD"
        )

        if self.current_cost_usd > self.max_cost_usd:
            raise BudgetExceededError(
                f"LLM cost budget exceeded. Current: ${self.current_cost_usd:.6f} USD, Limit: ${self.max_cost_usd:.6f} USD"
            )
