"""Regressão do BUG A — detecção de comparação falsa-positiva.

Cobre a correção em ``Comparator.detect_comparison_query`` que impedia
que uma única stopword ("or"/"ou"/"x") em queries longas fosse tratada
como comparação, gerando tabelas "Side-by-Side" absurdas (ex.: dividir
"...commits or pull requests" em duas colunas).

Ver: sessão 2026-07-16 (teste de estresse black_ops).
"""

from src.comparator import Comparator


LONG_TECHNICAL_QUERY = (
    "Correlate the technical root causes of concurrency bugs reported in real "
    "KuzuDB and DuckDB GitHub issues with recent academic literature on "
    "lock-free MVCC and determine whether the mitigations proposed in the "
    "papers have actually been adopted in the projects commits or pull requests"
)


def _c() -> Comparator:
    return Comparator()


def test_long_query_with_weak_or_is_not_comparative() -> None:
    """Query longa com 'or' de prosa NÃO deve virar comparação."""
    is_cmp, entities = _c().detect_comparison_query(LONG_TECHNICAL_QUERY)
    assert is_cmp is False
    assert entities == []


def test_plain_technical_query_is_not_comparative() -> None:
    is_cmp, _ = _c().detect_comparison_query(
        "how does MVCC work in embedded databases"
    )
    assert is_cmp is False


def test_explicit_vs_is_comparative() -> None:
    is_cmp, entities = _c().detect_comparison_query("Python vs Rust")
    assert is_cmp is True
    assert len(entities) >= 2
    assert any("python" in e.lower() for e in entities)
    assert any("rust" in e.lower() for e in entities)


def test_short_query_with_or_is_comparative() -> None:
    """'or' curto continua funcionando (ex.: 'Python or Rust for backend')."""
    is_cmp, entities = _c().detect_comparison_query("Python or Rust for backend")
    assert is_cmp is True
    assert len(entities) >= 2


def test_portuguese_melhor_ou_is_comparative() -> None:
    is_cmp, entities = _c().detect_comparison_query(
        "qual é melhor KuzuDB ou DuckDB"
    )
    assert is_cmp is True
    assert len(entities) >= 2


def test_empty_query_not_comparative() -> None:
    is_cmp, entities = _c().detect_comparison_query("")
    assert is_cmp is False
    assert entities == []
