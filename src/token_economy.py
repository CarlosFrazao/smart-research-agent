"""
token_economy.py — Otimizador de Tokens e Custos

Responsabilidades:
  1. Estimativa de tokens (tiktoken) para qualquer texto.
  2. Cálculo de custo por provedor/modelo.
  3. Truncamento inteligente: preserva início e fim do conteúdo.
  4. Budget enforcement: bloqueia chamadas que ultrapassariam o orçamento.

Uso:
    te = TokenEconomy()
    truncated = te.smart_truncate(huge_text, max_tokens=4096)
    cost = te.estimate_cost(text, model="gpt-4o-mini")

Skill: first-principles-thinking (Gestão de orçamentos e tokens)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Tabela de Preços (USD por 1K tokens) ─────────────────────────────────────
# Valores de referência; atualize conforme pricing oficial.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-4o":              {"input": 0.005,  "output": 0.015},
    "gpt-4o-mini":         {"input": 0.00015,"output": 0.0006},
    "gpt-4-turbo":         {"input": 0.01,   "output": 0.03},
    "gpt-3.5-turbo":       {"input": 0.0005, "output": 0.0015},
    # Google
    "gemini-2.5-pro":      {"input": 0.0035, "output": 0.0105},
    "gemini-2.5-flash":    {"input": 0.00015,"output": 0.0006},
    "gemini-1.5-pro":      {"input": 0.0025, "output": 0.005},
    "gemini-1.5-flash":    {"input": 0.000075,"output": 0.0003},
    # Anthropic
    "claude-opus-4":       {"input": 0.015,  "output": 0.075},
    "claude-sonnet-3.5":   {"input": 0.003,  "output": 0.015},
    "claude-haiku-3.5":    {"input": 0.0008, "output": 0.004},
    # Ollama (local — grátis)
    "ollama/llama3":       {"input": 0.0,    "output": 0.0},
    "ollama/mistral":      {"input": 0.0,    "output": 0.0},
}

# Encoding padrão quando o modelo não for reconhecido pelo tiktoken
DEFAULT_ENCODING = "cl100k_base"


def _get_encoding(model: str):
    """Retorna encoding tiktoken para o modelo; fallback para cl100k_base."""
    try:
        import tiktoken
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding(DEFAULT_ENCODING)
    except ImportError:
        return None


def _count_chars_approx(text: str) -> int:
    """Estimativa de tokens sem tiktoken: ~4 chars por token."""
    return max(1, len(text) // 4)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    query_hint: str = ""


@dataclass
class Budget:
    max_tokens_per_call: int = 8_000
    max_cost_usd_per_call: float = 0.05
    max_cost_usd_session: float = 2.00
    session_spent_usd: float = field(default=0.0, init=False)
    session_records: List[UsageRecord] = field(default_factory=list, init=False)

    def record(self, rec: UsageRecord) -> None:
        self.session_spent_usd += rec.estimated_cost_usd
        self.session_records.append(rec)

    def is_over_session_budget(self) -> bool:
        return self.session_spent_usd >= self.max_cost_usd_session

    def session_summary(self) -> Dict:
        return {
            "total_calls": len(self.session_records),
            "total_input_tokens": sum(r.input_tokens for r in self.session_records),
            "total_output_tokens": sum(r.output_tokens for r in self.session_records),
            "total_cost_usd": round(self.session_spent_usd, 6),
        }


# ── Motor Principal ──────────────────────────────────────────────────────────

class TokenEconomy:
    """
    Motor de economia de tokens — estimativa, custo, truncamento e budget.

    Parâmetros:
        default_model: modelo padrão para cálculos sem modelo especificado.
        budget: instância de Budget com limites configurados.
    """

    def __init__(
        self,
        default_model: str = "gpt-4o-mini",
        budget: Optional[Budget] = None,
    ) -> None:
        self.default_model = default_model
        self.budget = budget or Budget()

    # ── Contagem ─────────────────────────────────────────────────────────────

    def count_tokens(self, text: str, model: Optional[str] = None) -> int:
        """Conta tokens de `text` usando tiktoken (ou heurística 4 chars/token)."""
        enc = _get_encoding(model or self.default_model)
        if enc is None:
            return _count_chars_approx(text)
        return len(enc.encode(text))

    # ── Estimativa de Custo ───────────────────────────────────────────────────

    def estimate_cost(
        self,
        text: str,
        model: Optional[str] = None,
        output_tokens: int = 0,
    ) -> Tuple[int, float]:
        """
        Retorna (input_tokens, custo_USD) para o texto fornecido.
        Custo de output é calculado se `output_tokens` > 0.
        """
        m = (model or self.default_model).lower()
        input_tokens = self.count_tokens(text, model=m)
        pricing = MODEL_PRICING.get(m, {"input": 0.001, "output": 0.003})
        cost = (input_tokens / 1000) * pricing["input"]
        cost += (output_tokens / 1000) * pricing.get("output", 0.003)
        return input_tokens, round(cost, 8)

    # ── Truncamento Inteligente ───────────────────────────────────────────────

    def smart_truncate(
        self,
        text: str,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        head_ratio: float = 0.6,
    ) -> str:
        """
        Trunca `text` para no máximo `max_tokens` tokens.
        Preserva `head_ratio` do início e o restante do fim.

        Parâmetros:
            head_ratio: fração (0-1) que vai para o cabeçalho.
                        Ex: 0.6 → 60% início, 40% fim.
        """
        limit = max_tokens or self.budget.max_tokens_per_call
        enc = _get_encoding(model or self.default_model)

        if enc is None:
            # Heurística sem tiktoken
            char_limit = limit * 4
            if len(text) <= char_limit:
                return text
            head_chars = int(char_limit * head_ratio)
            tail_chars = char_limit - head_chars
            return (
                text[:head_chars]
                + "\n\n[... CONTEÚDO TRUNCADO ...]\n\n"
                + text[-tail_chars:]
            )

        tokens = enc.encode(text)
        if len(tokens) <= limit:
            return text

        head_len = int(limit * head_ratio)
        tail_len = limit - head_len

        head_tokens = tokens[:head_len]
        tail_tokens = tokens[-tail_len:]

        head_text = enc.decode(head_tokens)
        tail_text = enc.decode(tail_tokens)
        return head_text + "\n\n[... CONTEÚDO TRUNCADO ...]\n\n" + tail_text

    # ── Budget Enforcement ────────────────────────────────────────────────────

    def check_budget(self, text: str, model: Optional[str] = None) -> bool:
        """
        Retorna True se a chamada for dentro do orçamento.
        Loga aviso se próximo do limite.
        """
        if self.budget.is_over_session_budget():
            logger.warning(
                f"TokenEconomy: Orçamento de sessão esgotado "
                f"(${self.budget.session_spent_usd:.4f} / "
                f"${self.budget.max_cost_usd_session:.4f})"
            )
            return False

        tokens, cost = self.estimate_cost(text, model)

        if tokens > self.budget.max_tokens_per_call:
            logger.warning(
                f"TokenEconomy: Chamada excede max_tokens ({tokens} > "
                f"{self.budget.max_tokens_per_call}). Truncamento recomendado."
            )
            return False

        if cost > self.budget.max_cost_usd_per_call:
            logger.warning(
                f"TokenEconomy: Custo estimado ${cost:.4f} excede "
                f"limite por chamada ${self.budget.max_cost_usd_per_call:.4f}."
            )
            return False

        return True

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        model: Optional[str] = None,
        query_hint: str = "",
    ) -> UsageRecord:
        """Registra uso real de tokens após uma chamada LLM."""
        m = (model or self.default_model).lower()
        pricing = MODEL_PRICING.get(m, {"input": 0.001, "output": 0.003})
        cost = (input_tokens / 1000) * pricing["input"]
        cost += (output_tokens / 1000) * pricing.get("output", 0.003)
        rec = UsageRecord(
            model=m,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=round(cost, 8),
            query_hint=query_hint[:80],
        )
        self.budget.record(rec)
        return rec

    # ── Relatório ─────────────────────────────────────────────────────────────

    def session_summary(self) -> Dict:
        return self.budget.session_summary()

    def top_calls(self, n: int = 5) -> List[UsageRecord]:
        """Retorna os N calls mais caros da sessão."""
        return sorted(
            self.budget.session_records,
            key=lambda r: r.estimated_cost_usd,
            reverse=True,
        )[:n]
