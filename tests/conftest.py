import pytest
from unittest.mock import AsyncMock, MagicMock
from src.types import SearchResult, RankedResult, IntentResult, ExpandedQuery, Domain, Intention
from datetime import datetime


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
    client.generate = AsyncMock(return_value='{"domain": "saas_b2b", "entities": [], "intention": "discover", "urgency": "nao", "confidence": "alta"}')
    client.generate_structured = AsyncMock(return_value={
        "domain": "saas_b2b",
        "entities": ["HubSpot"],
        "intention": "discover",
        "urgency": "nao",
        "confidence": "alta",
    })
    return client
