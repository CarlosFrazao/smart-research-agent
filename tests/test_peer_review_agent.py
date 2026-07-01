import pytest
from unittest.mock import AsyncMock, MagicMock
from src.peer_review_agent import PeerReviewAgent, PeerReviewReport, ReviewIssue
from src.clients.llm_client import LLMClient

@pytest.mark.asyncio
async def test_peer_review_structured_success():
    llm_mock = MagicMock(spec=LLMClient)
    llm_mock.generate_structured = AsyncMock(return_value={
        "assessment": "strong",
        "confidence": 0.95,
        "issues": [
            {
                "category": "unsupported_claim",
                "severity": "minor",
                "description": "Falta de fonte primária na intro.",
                "location": "Esta é uma afirmação sem fonte.",
                "suggestion": "Adicionar citação."
            }
        ],
        "strengths": ["Boa organização."],
        "recommendations": ["Aprofundar análise."]
    })
    
    agent = PeerReviewAgent(llm_client=llm_mock)
    report = "# Relatório de Teste\nEsta é uma afirmação sem fonte. É a melhor ferramenta do mercado."
    
    review_report = await agent.review(report, results=[], query="test")
    
    assert review_report.overall_assessment == "strong"
    assert review_report.confidence_in_report == 0.95
    # Deve conter a issue do LLM e a issue heurística do superlativo "melhor"
    assert len(review_report.issues) >= 2
    categories = [i.category for i in review_report.issues]
    assert "bias" in categories
    assert "unsupported_claim" in categories

def test_peer_review_heuristic_superlative():
    llm_mock = MagicMock(spec=LLMClient)
    agent = PeerReviewAgent(llm_client=llm_mock)
    
    # "melhor" sem citação
    report = "Esta é a melhor opção."
    issues = agent._heuristic_review(report, [])
    
    assert len(issues) == 1
    assert issues[0].category == "bias"
    assert issues[0].severity == "major"
    assert "melhor" in issues[0].description

def test_peer_review_heuristic_short_section():
    llm_mock = MagicMock(spec=LLMClient)
    agent = PeerReviewAgent(llm_client=llm_mock)
    
    # Seção com menos de 200 caracteres de conteúdo
    report = "## Seção Introdução\nConteúdo curto."
    issues = agent._heuristic_review(report, [])
    
    assert len(issues) == 1
    assert issues[0].category == "missing_context"
    assert issues[0].severity == "minor"
    assert "Seção Introdução" in issues[0].description

def test_to_markdown():
    llm_mock = MagicMock(spec=LLMClient)
    agent = PeerReviewAgent(llm_client=llm_mock)
    
    report = PeerReviewReport(
        overall_assessment="weak",
        confidence_in_report=0.45,
        issues=[
            ReviewIssue(
                category="logical_fallacy",
                severity="critical",
                description="Afirmação contraditória.",
                location="A é B mas B não é A.",
                suggestion="Rever coerência lógica."
            )
        ],
        strengths=["Algum conteúdo útil."],
        recommendations=["Refazer pesquisa."]
    )
    
    md = agent.to_markdown(report)
    assert "## 🔍 Revisão Científica (Peer Review Agent)" in md
    assert "Fraco" in md
    assert "logical_fallacy" in md
    assert "Afirmação contraditória." in md
