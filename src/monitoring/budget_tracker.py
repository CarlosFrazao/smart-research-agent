"""Budget Tracker — Monitora e limita o custo financeiro das chamadas a LLMs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Erro lançado quando o budget financeiro de chamadas LLM é ultrapassado."""

    pass


@dataclass
class _SourceCostEntry:
    """Custo e performance acumulados de uma fonte de busca dentro de uma sessão."""

    requests: int = 0
    tokens: int = 0
    latency_sum_ms: float = 0.0
    latency_samples: int = 0


class BudgetTracker:
    """Rastreia e limita o custo das chamadas LLM em USD."""

    def __init__(self, max_cost_usd: float = 10.0):
        self.max_cost_usd = max_cost_usd
        self.current_cost_usd = 0.0
        # sessão -> {fonte -> acumulador de custo/performance}
        self._source_costs: Dict[str, Dict[str, _SourceCostEntry]] = {}

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

    def record_source_cost(
        self,
        source_name: str,
        session_id: str,
        tokens_used: int = 0,
        requests_made: int = 1,
        latency_ms: float = 0.0,
    ) -> None:
        """Registra o custo/performance de uma chamada a uma fonte de busca específica.

        Args:
            source_name: Identificador da fonte (ex: ``"github"``, ``"firecrawl"``).
            session_id: Identificador da sessão de pesquisa para agrupamento.
            tokens_used: Tokens consumidos nesta chamada.
            requests_made: Quantidade de requisições agregadas neste registro.
            latency_ms: Latência observada da chamada (milissegundos).
        """
        session = self._source_costs.setdefault(session_id, {})
        entry = session.get(source_name)
        if entry is None:
            entry = _SourceCostEntry()
            session[source_name] = entry

        entry.requests += max(requests_made, 0)
        entry.tokens += max(tokens_used, 0)
        if latency_ms > 0:
            entry.latency_sum_ms += latency_ms
            entry.latency_samples += 1

        logger.debug(
            "BudgetTracker: custo de fonte registrado — sessão=%s fonte=%s "
            "requests=%d tokens=%d latency_ms=%.1f",
            session_id,
            source_name,
            entry.requests,
            entry.tokens,
            latency_ms,
        )

    def get_source_cost_summary(self, session_id: str) -> Dict[str, Dict[str, float]]:
        """Retorna o custo e a performance acumulados por fonte em uma sessão.

        Args:
            session_id: Identificador da sessão de pesquisa.

        Returns:
            Dicionário mapeando cada fonte para suas métricas acumuladas::

                {
                    "github": {"requests": 5, "tokens": 200, "avg_latency_ms": 120.0},
                    "firecrawl": {"requests": 3, "tokens": 800, "avg_latency_ms": 2100.0},
                }

            Fontes sem amostras de latência reportam ``avg_latency_ms=0.0``.
        """
        session = self._source_costs.get(session_id, {})
        summary: Dict[str, Dict[str, float]] = {}
        for source_name, entry in session.items():
            avg_latency = (
                entry.latency_sum_ms / entry.latency_samples
                if entry.latency_samples > 0
                else 0.0
            )
            summary[source_name] = {
                "requests": float(entry.requests),
                "tokens": float(entry.tokens),
                "avg_latency_ms": round(avg_latency, 3),
            }
        return summary
