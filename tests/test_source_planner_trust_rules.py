"""Testes de integração do TrustRuleStore no SourcePlanner."""

import pytest
from src.source_planner import SourcePlanner
from src.types import Domain, ExpandedQuery, IntentResult, Intention


@pytest.fixture
def planner():
    """Instancia SourcePlanner com LLM None (roteamento estático)."""
    return SourcePlanner(llm=None)


@pytest.fixture
def ai_ml_intent():
    """Intent de domínio AI_ML (primary: arxiv, github, notion, rss, searxng)."""
    return IntentResult(
        domain=Domain.AI_ML,
        entities=[],
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )


@pytest.fixture
def queries():
    """Lista de queries expandidas para o planner."""
    return [
        ExpandedQuery(query="test query", type="original", priority="alta"),
        ExpandedQuery(query="machine learning", type="synonym", priority="media"),
    ]


class TestSourcePlannerTrustRules:
    """Valida a aplicação de regras allow/deny no plano de fontes."""

    def test_trust_rules_deny_removes_source(self, planner, ai_ml_intent, queries):
        """Regra 'deny' remove a fonte do primary e secondary."""
        context = {"extra": {"trust_rules": {"arxiv": "deny"}}}

        plan = planner.plan(ai_ml_intent, queries, context)

        assert "arxiv" not in plan.primary
        assert "arxiv" not in plan.secondary

    def test_trust_rules_allow_promotes_to_top(self, planner, ai_ml_intent, queries):
        """Regra 'allow' promove a fonte para o topo do primary.

        A fonte ausente no primary (reddit, que está no secondary de ai_ml)
        é inserida no topo de primary. A spec não a remove do secondary.
        """
        context = {"extra": {"trust_rules": {"reddit": "allow"}}}

        plan = planner.plan(ai_ml_intent, queries, context)

        assert plan.primary[0] == "reddit"

    def test_trust_rules_deny_and_allow_combined(self, planner, ai_ml_intent, queries):
        """Combina deny (remove) e allow (promove fonte ausente no primary)."""
        # arxiv (primary) -> deny ; reddit (secondary) -> allow
        context = {"extra": {"trust_rules": {"arxiv": "deny", "reddit": "allow"}}}

        plan = planner.plan(ai_ml_intent, queries, context)

        # arxiv removido do primary e secondary
        assert "arxiv" not in plan.primary
        assert "arxiv" not in plan.secondary
        # reddit (antes no secondary) é promovido ao topo do primary
        assert plan.primary[0] == "reddit"

    def test_trust_rules_empty_no_change(self, planner, ai_ml_intent, queries):
        """Sem regras, o plano permanece inalterado."""
        context = {"extra": {"trust_rules": {}}}

        plan = planner.plan(ai_ml_intent, queries, context)

        # ai_ml primary padrão: arxiv, github, notion, rss, searxng
        assert plan.primary[:5] == ["arxiv", "github", "notion", "rss", "searxng"]

    def test_trust_rules_missing_context_no_change(self, planner, ai_ml_intent, queries):
        """Sem context, o plano permanece inalterado (backward compatibility)."""
        plan = planner.plan(ai_ml_intent, queries)  # context=None

        assert plan.primary[:5] == ["arxiv", "github", "notion", "rss", "searxng"]

    def test_trust_rules_universal_llm_fallback(self, planner, queries):
        """No domínio 'universal' com LLM=None, as regras still são aplicadas."""
        # Usa ai_ml (github é primary) e testa deny/allow
        intent = IntentResult(
            domain=Domain.AI_ML,
            entities=[],
            intention=Intention.DISCOVER,
            urgency="nao",
            confidence="alta",
        )
        context = {"extra": {"trust_rules": {"github": "deny", "reddit": "allow"}}}

        plan = planner.plan(intent, queries, context)

        assert "github" not in plan.primary
        assert "github" not in plan.secondary
        assert plan.primary[0] == "reddit"
