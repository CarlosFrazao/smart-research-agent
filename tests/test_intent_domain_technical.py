"""Onda 1 / M1.2 — classificação de domínio robusta (BUG D).

Antes da correção, ``_heuristic_domain`` usava correspondência de SUBSTRING
(``kw in query``), o que gerava falsos positivos ("free" ⊂ "lock-free",
"git" ⊂ "github", "ai" ⊂ "domain") e, em empate, escolhia ``saas_b2b`` por
ser o primeiro no dict. Uma query técnica de banco de dados era classificada
como comercial (saas_b2b), degradando o plano de fontes e o estilo de citação.

A correção: (1) matching por fronteira de palavra; (2) vocabulário técnico
expandido (database/concurrency); (3) desempate que prioriza domínios
técnicos sobre saas_b2b.

Ver: Plan_SRA_Melhorias_Qualidade_2026-07-16.md
"""

from unittest.mock import MagicMock

from src.intent_analyzer import IntentAnalyzer
from src.types import Domain


def _analyzer() -> IntentAnalyzer:
    return IntentAnalyzer(llm_client=MagicMock())


HARD_QUERY = (
    "Correlate the technical root causes of concurrency bugs (writer "
    "starvation, deadlock, lock contention) reported in real KuzuDB and "
    "DuckDB GitHub issues with recent academic literature on lock-free MVCC "
    "in embedded databases, and determine whether the mitigations proposed "
    "in the papers have actually been adopted in the projects' commits or "
    "pull requests"
)


def test_hard_db_query_is_not_saas_b2b() -> None:
    """A query difícil NÃO deve ser classificada como saas_b2b."""
    domain = _analyzer()._heuristic_domain(HARD_QUERY)
    assert domain != Domain.SAAS_B2B
    # Deve cair num domínio técnico plausível.
    assert domain in {
        Domain.OPEN_SOURCE,
        Domain.DEV_TOOLS,
        Domain.INFRASTRUCTURE,
    }


def test_lockfree_does_not_match_free_saas_keyword() -> None:
    """'lock-free' não pode disparar a keyword 'free' de saas_b2b."""
    domain = _analyzer()._heuristic_domain("lock-free data structures in Rust")
    assert domain != Domain.SAAS_B2B


def test_genuine_saas_query_still_classifies_saas() -> None:
    """Query B2B genuína continua sendo saas_b2b."""
    domain = _analyzer()._heuristic_domain(
        "best CRM and helpdesk SaaS pricing for B2B sales teams"
    )
    assert domain == Domain.SAAS_B2B


def test_database_vocabulary_is_recognized() -> None:
    """Termos de banco de dados são reconhecidos como técnicos."""
    domain = _analyzer()._heuristic_domain(
        "MVCC and deadlock handling in embedded databases"
    )
    assert domain != Domain.SAAS_B2B
    assert domain != Domain.GENERAL


def test_ai_query_still_ai_ml() -> None:
    domain = _analyzer()._heuristic_domain(
        "transformer LLM embedding models for retrieval"
    )
    assert domain == Domain.AI_ML


def test_word_boundary_git_does_not_falsely_win() -> None:
    """'github' deve contar para open_source, não 'git' isolado dentro dele."""
    # Query com github mas sem 'git' isolado: open_source deve vencer.
    domain = _analyzer()._heuristic_domain(
        "popular open source library on github for parsing"
    )
    assert domain == Domain.OPEN_SOURCE
