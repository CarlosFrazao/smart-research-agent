"""
Testes de roteamento por MODO de operação no SourcePlanner (M1 — modo 'mito').

Valida que quando o SourcePlanner recebe um modo com searchers explícitos
(ex: 'mito' → [web, searxng, wikipedia, snopes, reddit]), essas fontes se
tornam primárias no plano de busca, independentemente do domínio detectado
pelo IntentAnalyzer. O domínio detectado vira secundário (complemento).
"""

import pytest
from src.source_planner import SourcePlanner, SourcePlan
from src.types import ExpandedQuery, IntentResult, Domain


def _make_intent(domain: str = "general") -> IntentResult:
    """Cria um IntentResult mínimo com domínio informado."""
    return IntentResult(
        domain=Domain[domain.upper()]
        if hasattr(Domain, domain.upper())
        else Domain.GENERAL,
        intention="discover",
        urgency="nao",
        confidence="media",
        entities=[],
    )


def _make_queries(n: int = 3) -> list[ExpandedQuery]:
    """Cria N ExpandedQuery dummy."""
    return [
        ExpandedQuery(query=f"q{i}", type="synonym", priority="alta") for i in range(n)
    ]


class TestSourcePlannerModeRouting:
    """O modo deve sobrepor o domínio no plano de fontes."""

    def test_mito_mode_overrides_domain_sources(self):
        """Modo 'mito' deve colocar web/searxng/wikipedia/snopes/reddit como primárias."""
        planner = SourcePlanner(mode="mito")
        intent = _make_intent("general")
        queries = _make_queries()

        plan = planner.plan(intent, queries, None)

        assert isinstance(plan, SourcePlan)
        # Primárias devem ser exatamente as do modo 'mito'
        assert plan.primary == ["web", "searxng", "wikipedia", "snopes", "reddit"]
        # O domínio 'general' deve aparecer como secundário (sem duplicar primárias)
        assert "github" in plan.secondary
        assert "web" not in plan.secondary  # já está em primary
        # Plano deve conter entradas para todas as fontes
        assert set(plan.sources.keys()) == set(plan.primary + plan.secondary)

    def test_unknown_mode_falls_back_to_domain(self):
        """Modo inexistente deve cair no fallback 'cirurgia' (sem quebrar)."""
        planner = SourcePlanner(mode="modo_que_nao_existe")
        intent = _make_intent("general")
        queries = _make_queries()

        plan = planner.plan(intent, queries, None)

        # OperationModes.get_mode faz fallback para 'cirurgia'
        # cirurgia → searchers [web, arxiv, github, stackoverflow, hackernews, reddit, serpapi]
        assert "arxiv" in plan.primary
        assert "github" in plan.primary
        # 'snopes' não está em cirurgia → não deve ser primário
        assert "snopes" not in plan.primary

    def test_none_mode_uses_domain(self):
        """Sem modo (None), comporta-se como antes (roteamento por domínio)."""
        planner = SourcePlanner(mode=None)
        intent = _make_intent("ai_ml")
        queries = _make_queries()

        plan = planner.plan(intent, queries, None)

        # ai_ml → primary inclui arxiv, github, notion, rss, searxng
        assert "arxiv" in plan.primary
        assert "snopes" not in plan.primary  # não é do modo mito

    def test_mode_routing_is_logged(self, caplog):
        """O log deve indicar que o modo sobrepôs as fontes."""
        import logging

        planner = SourcePlanner(mode="mito")
        intent = _make_intent("general")
        queries = _make_queries()

        with caplog.at_level(logging.INFO):
            planner.plan(intent, queries, None)

        assert any("mito" in rec.message for rec in caplog.records)

    def test_mito_mode_primary_order_preserved(self):
        """Ordem das primárias do modo 'mito' deve ser preservada."""
        planner = SourcePlanner(mode="mito")
        intent = _make_intent("general")
        queries = _make_queries()

        plan = planner.plan(intent, queries, None)

        assert plan.primary == ["web", "searxng", "wikipedia", "snopes", "reddit"]
