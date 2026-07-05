import pytest
from src.research_auditor import ResearchAuditor, AuditClaim
from src.types import SearchResult


@pytest.mark.asyncio
async def test_research_auditor_granular_validation():
    # Setup mock LLM and auditor
    from unittest.mock import MagicMock

    llm = MagicMock()
    auditor = ResearchAuditor(llm_client=llm)

    # Prepare claims
    claims = [
        AuditClaim(
            text="O python 3.12 introduziu melhorias de performance significativas no interpretador."
        ),
        AuditClaim(
            text="O docker desktop no windows wsl2 consome muita memória ram por padrão."
        ),
        AuditClaim(
            text="Esta afirmação não terá nenhuma evidência nas fontes mockadas."
        ),
    ]

    # Prepare mock search results (sources)
    results = [
        SearchResult(
            source="github",
            title="Python 3.12 performance optimization changelog",
            url="https://github.com/python/cpython/pulls/3.12",
            description="This pull request introduces faster interpreter loop and memory optimizations in python 3.12, increasing general performance.",
        ),
        SearchResult(
            source="reddit",
            title="Python 3.12 is faster than 3.11",
            url="https://reddit.com/r/python/3.12-faster",
            description="Benchmarks show that python 3.12 runs significantly faster due to PEP 709 inline comprehensions and interpreter performance tweaks.",
        ),
        SearchResult(
            source="medium",
            title="Docker Desktop WSL2 memory leak workaround",
            url="https://medium.com/docker-wsl2-memory",
            description="Users report that Docker Desktop Desktop WSL2 vmem/vmmem processes consume large amounts of memory ram on Windows.",
        ),
    ]

    # Run claim validation
    validated_claims = await auditor._validate_claims(claims, results)

    # Claim 1: "O python 3.12..." should have 2 unique sources (github, reddit) -> status: verified
    claim_python = validated_claims[0]
    assert claim_python.status == "verified"
    assert len(claim_python.supporting_urls) == 2
    assert len(claim_python.evidence_snippets) == 2
    assert claim_python.needs_recheck is False

    # Claim 2: "O docker desktop..." should have 1 unique source (medium) -> status: single_source
    claim_docker = validated_claims[1]
    assert claim_docker.status == "single_source"
    assert len(claim_docker.supporting_urls) == 1
    assert claim_docker.needs_recheck is False

    # Claim 3: "Esta afirmação..." should have 0 matches -> status: low_confidence
    claim_unverified = validated_claims[2]
    assert claim_unverified.status == "low_confidence"
    assert len(claim_unverified.supporting_urls) == 0
    assert claim_unverified.needs_recheck is True


def test_research_auditor_notes_injection():
    # Setup mock LLM and auditor
    from unittest.mock import MagicMock

    llm = MagicMock()
    auditor = ResearchAuditor(llm_client=llm)

    report_text = "# Relatório de Tecnologia\n\nEste é o corpo do relatório."
    claims = [
        AuditClaim(
            text="Fato A",
            status="verified",
            confidence=0.85,
            evidence_snippets=["Snippet A"],
            supporting_urls=["http://fatoa.com"],
        ),
        AuditClaim(
            text="Fato B",
            status="single_source",
            confidence=0.55,
            evidence_snippets=["Snippet B"],
            supporting_urls=["http://fatob.com"],
        ),
    ]

    enriched = auditor._inject_audit_notes(report_text, claims)

    # Check that it contains status symbols and correct headings
    assert "## 🛡️ Auditoria de Claims (ResearchAuditor)" in enriched
    assert "### 🔍 Detalhamento das Provas Factuais (Claim-by-Claim)" in enriched
    assert "✅ Fato A" in enriched
    assert "🟡 Fato B" in enriched
    assert "[Fonte](http://fatoa.com)" in enriched
    assert '*"Snippet B"*' in enriched
