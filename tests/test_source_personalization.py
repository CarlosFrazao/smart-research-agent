"""Testes da Fase 4 — personalização de fontes por histórico de feedback do usuário.

Cobertura:
- FeedbackStore.record_source_feedback / get_source_weights (pesos neutros, volume mínimo, clamp)
- SourcePlanner._apply_user_weights (reordenação conforme histórico do usuário)
"""

import pytest

from src.feedback_store import FeedbackStore
from src.source_planner import SourcePlanner
from src.types import Domain, ExpandedQuery, Intention, SourcePlan


def _store(tmp_path) -> FeedbackStore:
    """Cria um FeedbackStore isolado em diretório temporário."""
    return FeedbackStore(store_path=str(tmp_path / "_feedback.jsonl"))


def _record_many(
    store: FeedbackStore,
    user_id: str,
    source: str,
    domain: str,
    useful: bool,
    count: int,
) -> None:
    """Registra `count` feedbacks de fonte com o mesmo sinal útil/ignorado."""
    for _ in range(count):
        store.record_source_feedback(
            user_id=user_id,
            source_name=source,
            query_domain=domain,
            was_useful=useful,
        )


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


# ── FeedbackStore ──────────────────────────────────────────────────────────


def test_cold_start_returns_equal_weights(store):
    """Usuário sem histórico → todas as fontes têm peso 1.0."""
    sources = ["github", "arxiv", "wikipedia"]
    weights = store.get_source_weights("cold_user", "dev_tools", sources)
    assert weights == {"github": 1.0, "arxiv": 1.0, "wikipedia": 1.0}
    assert all(w == 1.0 for w in weights.values())


def test_useful_source_gets_higher_weight(store):
    """Fonte com 100% de aprovação → weight > 1.0."""
    _record_many(store, "u1", "github", "dev_tools", useful=True, count=10)
    weights = store.get_source_weights("u1", "dev_tools", ["github", "arxiv"])
    assert weights["github"] > 1.0
    assert weights["github"] == 2.0  # clamp máximo


def test_ignored_source_gets_lower_weight(store):
    """Fonte com 0% de aprovação → weight < 1.0."""
    _record_many(store, "u1", "arxiv", "dev_tools", useful=False, count=10)
    weights = store.get_source_weights("u1", "dev_tools", ["github", "arxiv"])
    assert weights["arxiv"] < 1.0
    assert weights["arxiv"] == 0.2  # clamp mínimo


def test_minimum_volume_threshold(store):
    """Com menos de 5 feedbacks, peso permanece 1.0 (anti-overfitting)."""
    _record_many(store, "u1", "github", "dev_tools", useful=True, count=4)
    weights = store.get_source_weights("u1", "dev_tools", ["github", "arxiv"])
    assert weights["github"] == 1.0  # volume insuficiente → neutro


def test_weight_clamped_to_limits(store):
    """Peso nunca ultrapassa 2.0 nem fica abaixo de 0.2."""
    _record_many(store, "u1", "github", "dev_tools", useful=True, count=10)
    _record_many(store, "u1", "arxiv", "dev_tools", useful=False, count=10)
    weights = store.get_source_weights("u1", "dev_tools", ["github", "arxiv"])
    assert weights["github"] == 2.0
    assert weights["arxiv"] == 0.2
    assert all(0.2 <= w <= 2.0 for w in weights.values())


def test_weights_are_domain_isolated(store):
    """Pesos de um domínio não vazam para outro domínio."""
    _record_many(store, "u1", "github", "dev_tools", useful=True, count=10)
    weights = store.get_source_weights("u1", "ai_ml", ["github", "arxiv"])
    assert weights["github"] == 1.0  # sem histórico em ai_ml → neutro


# ── SourcePlanner ────────────────────────────────────────────────────────────


def _make_intent(domain: Domain) -> object:
    return type(
        "Intent",
        (),
        {
            "domain": domain,
            "entities": [],
            "intention": Intention.DISCOVER,
            "urgency": "nao",
            "confidence": "alta",
        },
    )()


def test_source_order_changes_with_weights(store, tmp_path):
    """Fontes reordenadas corretamente pelo planner conforme o histórico."""
    user_id = "order_user"
    # stackoverflow altamente útil; github altamente ignorado em dev_tools.
    _record_many(store, user_id, "stackoverflow", "dev_tools", useful=True, count=10)
    _record_many(store, user_id, "github", "dev_tools", useful=False, count=10)

    planner = SourcePlanner(feedback_store=store, user_id=user_id)
    intent = _make_intent(Domain.DEV_TOOLS)
    queries = [
        ExpandedQuery(query="python orm", type="qualificador", priority="alta"),
    ]
    plan: SourcePlan = planner.plan(intent, queries)

    # stackoverflow (útil) deve aparecer antes de github (ignorado) na lista primária.
    assert "stackoverflow" in plan.primary
    assert "github" in plan.primary
    assert plan.primary.index("stackoverflow") < plan.primary.index("github")


def test_no_personalization_when_no_feedback_store(tmp_path):
    """Sem feedback_store/user_id → ordem padrão do domínio é preservada."""
    planner = SourcePlanner()  # sem feedback_store, sem user_id
    intent = _make_intent(Domain.DEV_TOOLS)
    queries = [
        ExpandedQuery(query="python orm", type="qualificador", priority="alta"),
    ]
    plan: SourcePlan = planner.plan(intent, queries)
    # Ordem estática esperada para dev_tools (primeiro da lista primária).
    assert plan.primary[0] == "github"
