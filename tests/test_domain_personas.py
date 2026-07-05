import pytest
from datetime import datetime
from src.domain_personas import DomainPersona
from src.types import SearchResult, Domain


def test_domain_personas_apa():
    # APA formatter chosen for SAAS_B2B domain
    persona = DomainPersona(Domain.SAAS_B2B)

    result = SearchResult(
        source="reddit",
        title="Best SaaS architecture patterns",
        url="https://reddit.com/r/saas/patterns",
        description="A list of patterns",
        metrics={"author": "John Doe"},
        raw={"published_date": "2024-05-15"},
    )

    citation = persona.format_citation(result, index=1)

    assert "[1] John Doe. (2024). *Best SaaS architecture patterns* REDDIT." in citation
    assert "Disponível em: https://reddit.com/r/saas/patterns" in citation


def test_domain_personas_ieee():
    # IEEE formatter chosen for DEV_TOOLS domain
    persona = DomainPersona(Domain.DEV_TOOLS)

    result = SearchResult(
        source="github",
        title="awesome-dev-tools repo",
        url="https://github.com/awesome/dev-tools",
        description="Curated list of dev tools",
        raw={
            "owner": {"login": "awesome-user"},
            "created_at": 1714867200,
        },  # timestamp for 2024
    )

    citation = persona.format_citation(result, index=3)

    assert '[3] awesome-user, "awesome-dev-tools repo", GITHUB, 2024.' in citation
    assert "[Online]. Available: https://github.com/awesome/dev-tools" in citation


def test_domain_personas_bluebook():
    persona = DomainPersona(Domain.GENERAL)

    result = SearchResult(
        source="lexis",
        title="Legal Software Licensing terms",
        url="https://lexis.com/licensing",
        metrics={"author": "Supreme Court"},
        raw={"published_date": "July 4, 2026"},
    )

    citation = persona.format_bluebook(result, index=5)

    assert (
        "[5] Supreme Court, Legal Software Licensing terms, LEXIS (July 4, 2026)"
        in citation
    )
    assert "https://lexis.com/licensing" in citation


def test_domain_personas_missing_metadata():
    persona = DomainPersona(Domain.GENERAL)

    # Missing author and date
    result = SearchResult(
        source="web",
        title="No Author No Date Article",
        url="https://example.com/no-author",
        fetched_at=datetime(2025, 8, 12),
    )

    citation = persona.format_citation(result, index=2)

    # For APA with missing author, title becomes the author
    assert "[2] No Author No Date Article. (2025). WEB." in citation
    assert "Disponível em: https://example.com/no-author" in citation
