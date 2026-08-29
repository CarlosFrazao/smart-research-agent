"""
conftest.py — Configuração global do pytest para o Smart Research Agent.

Inclui:
- Fixtures compartilhadas (sample_search_result, mock_llm_client, etc.)
- Guard de coleta para dependências pesadas (chromadb/pyarrow/sentence_transformers/kuzu)
  que podem crashar no import em ambientes Windows por conflito de DLLs nativas.
"""
from __future__ import annotations

import importlib
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.types import (
    SearchResult,
    RankedResult,
    IntentResult,
    ExpandedQuery,
    Domain,
    Intention,
)

# ---------------------------------------------------------------------------
# Guard de dependências pesadas — previne crash de coleta no Windows
# ---------------------------------------------------------------------------

#: Pacotes que podem crashar no import por conflitos de DLL nativa no Windows
#: (pyarrow puxado transitivamente pelo chromadb, sentence-transformers com torch, etc.)
_HEAVY_PACKAGES = ["chromadb", "pyarrow", "sentence_transformers", "kuzu", "torch"]

_HEAVY_AVAILABLE: dict[str, bool] = {}

for _pkg in _HEAVY_PACKAGES:
    try:
        importlib.import_module(_pkg)
        _HEAVY_AVAILABLE[_pkg] = True
    except Exception:  # noqa: BLE001
        _HEAVY_AVAILABLE[_pkg] = False


def _all_heavy_available() -> bool:
    return all(_HEAVY_AVAILABLE.values())


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    """Pula automaticamente testes marcados com @pytest.mark.heavy quando
    as dependências pesadas não estão disponíveis neste ambiente."""
    if _all_heavy_available():
        return  # Tudo ok — não pula nada

    unavailable = [pkg for pkg, ok in _HEAVY_AVAILABLE.items() if not ok]
    reason = (
        f"Dependências pesadas indisponíveis neste ambiente "
        f"({', '.join(unavailable)}). Marque o teste com "
        f"@pytest.mark.heavy para pular automaticamente."
    )
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("heavy"):
            item.add_marker(skip_marker)


def pytest_configure(config: pytest.Config) -> None:
    """Registra os markers 'heavy' e 'integration' para evitar warnings desconhecidos."""
    config.addinivalue_line(
        "markers",
        "heavy: testes que requerem chromadb/pyarrow/sentence_transformers/kuzu. "
        "Pulados automaticamente quando essas libs não importam corretamente.",
    )
    config.addinivalue_line(
        "markers",
        "integration: testes de integração que usam buscadoras reais e/ou "
        "fazem chamadas de rede. Excluídos da CI padrão (`pytest -m 'not integration'`).",
    )


# ---------------------------------------------------------------------------
# Fixtures compartilhadas
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_search_result():
    return SearchResult(
        source="github",
        title="twenty/twenty",
        url="https://github.com/twentyhq/twenty",
        description="A CRM open source moderno",
        metrics={"stars": 5000, "forks": 300, "language": "TypeScript"},
    )


@pytest.fixture
def sample_ranked_result(sample_search_result):
    return RankedResult(
        source=sample_search_result.source,
        title=sample_search_result.title,
        url=sample_search_result.url,
        description=sample_search_result.description,
        metrics=sample_search_result.metrics,
        raw=sample_search_result.raw,
        fetched_at=sample_search_result.fetched_at,
        score=75.0,
        score_breakdown={"base_score": 75.0},
    )


@pytest.fixture
def sample_intent():
    return IntentResult(
        domain=Domain.SAAS_B2B,
        entities=["HubSpot", "CRM"],
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )


@pytest.fixture
def sample_expanded_query():
    return ExpandedQuery(
        query="open source CRM",
        type="qualificador",
        priority="alta",
        rationale="encontra projetos open source",
    )


@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    client.generate = AsyncMock(
        return_value='{"domain": "saas_b2b", "entities": [], "intention": "discover", "urgency": "nao", "confidence": "alta"}'
    )
    client.generate_structured = AsyncMock(
        return_value={
            "domain": "saas_b2b",
            "entities": ["HubSpot"],
            "intention": "discover",
            "urgency": "nao",
            "confidence": "alta",
        }
    )
    return client
