import pytest
from unittest.mock import AsyncMock, MagicMock
from src.research_auditor import ResearchAuditor, AuditClaim
from src.types import SearchResult


@pytest.mark.asyncio
async def test_validate_claims_granular_llm():
    # Setup mock LLM Client
    mock_llm = MagicMock()
    mock_llm.generate_structured = AsyncMock()

    # Mock data to return from LLM
    mock_llm.generate_structured.return_value = {
        "SRA v3.0 possui interface de concorrência.": {
            "status": "verified",
            "confidence": 0.95,
            "supporting_snippets": [
                "O SRA v3.0 implementa o HITLManager com asyncio.Event."
            ],
            "sources": ["https://github.com/CarlosFrazao/smart-research-agent"],
        }
    }

    auditor = ResearchAuditor(llm_client=mock_llm)

    claims = [AuditClaim(text="SRA v3.0 possui interface de concorrência.")]
    results = [
        SearchResult(
            title="SRA Release Notes",
            url="https://github.com/CarlosFrazao/smart-research-agent",
            description="O SRA v3.0 implementa o HITLManager com asyncio.Event para suspensão assíncrona.",
            source="web",
        )
    ]

    validated_claims = await auditor._validate_claims(claims, results)

    # Assertions
    assert len(validated_claims) == 1
    assert validated_claims[0].status == "verified"
    assert validated_claims[0].confidence == 0.95
    assert validated_claims[0].supporting_snippets == [
        "O SRA v3.0 implementa o HITLManager com asyncio.Event."
    ]
    assert validated_claims[0].supporting_sources == [
        "https://github.com/CarlosFrazao/smart-research-agent"
    ]
    assert validated_claims[0].needs_recheck is False


@pytest.mark.asyncio
async def test_validate_claims_fallback_heuristic():
    # Setup mock LLM Client that throws an exception to force fallback
    mock_llm = MagicMock()
    mock_llm.generate_structured = AsyncMock(side_effect=RuntimeError("LLM error"))

    auditor = ResearchAuditor(llm_client=mock_llm)

    claims = [AuditClaim(text="O SRA utiliza ChromaDB.")]
    results = [
        SearchResult(
            title="ChromaDB integration",
            url="https://chromadb.com",
            description="O SRA se conecta a uma instância local do ChromaDB para armazenar embeddings.",
            source="web",
        )
    ]

    validated_claims = await auditor._validate_claims(claims, results)

    # Heuristic matches words like "SRA", "ChromaDB"
    assert len(validated_claims) == 1
    # Because ChromaDB and SRA match with high coverage, status should be verified or single_source
    assert validated_claims[0].status in ("verified", "single_source")
    assert len(validated_claims[0].supporting_snippets) > 0
    assert len(validated_claims[0].supporting_sources) > 0


def test_inject_audit_notes():
    auditor = ResearchAuditor(llm_client=MagicMock())
    claims = [
        AuditClaim(
            text="SRA v3.0 possui concorrência.",
            status="verified",
            confidence=0.9,
            supporting_snippets=["Snippet de teste"],
            supporting_sources=["https://source.com"],
        )
    ]
    report_text = "# Relatório de Pesquisa\nConteúdo."
    enriched = auditor._inject_audit_notes(report_text, claims)

    assert "## 🛡️ Auditoria de Claims" in enriched
    assert "Evidências Factuais Encontradas" in enriched
    assert "Snippet de teste" in enriched
    assert "https://source.com" in enriched
