import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pipeline.stages.report_stage import ReportStage
from src.pipeline.pipeline import PipelineContext
from src.types import ResearchMetadata, SynthesizedResult

@pytest.mark.asyncio
async def test_report_stage_consolidated_success():
    # Setup mocks
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="""{
        "executive_summary": "Resumo executivo simulado com dados concretos e válidos para ultrapassar os cinquenta caracteres exigidos pelo validador.",
        "recommendation": "Recomendação simulada principal e alternativas estratégicas com detalhes adicionais do projeto para passar a regra de comprimento do validador.",
        "trends": "Tendências tecnológicas simuladas com evidência concreta de mercado e adoção de comunidade de desenvolvedores ativos."
    }""")

    # Mock dos analisadores
    mock_temporal = MagicMock()
    mock_temporal.generate_timeline_section.return_value = "Timeline Markdown"

    mock_sentiment = MagicMock()
    mock_sentiment.generate_sentiment_section.return_value = "Sentiment Markdown"

    mock_comparator = MagicMock()
    mock_comparator.generate_comparison_section.return_value = "Comparison Markdown"

    with patch("src.temporal_analyzer.TemporalAnalyzer", return_value=mock_temporal), \
         patch("src.sentiment_analyzer.SentimentAnalyzer", return_value=mock_sentiment), \
         patch("src.comparator.Comparator", return_value=mock_comparator):

        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        stage = ReportStage(llm_client=mock_llm, cache=mock_cache)

        # Resultados sintetizados de entrada
        results = [
            SynthesizedResult(
                entity="entity_a",
                title="Project A",
                description="A great project",
                sources=["github"],
                combined_score=0.9,
                highlights=["fast"],
                metrics={"stars": 1000},
            )
        ]
        from datetime import datetime
        metadata = ResearchMetadata(
            query="test query",
            timestamp=datetime.now(),
            domain="general",
            sources=["github"],
            total_results=1,
            iterations=1,
            overall_confidence=0.95,
            low_confidence_warnings=[],
        )

        context = PipelineContext(query="test query")
        context.synthesized_results = results
        context.metadata = metadata

        # Executa e verifica dentro do bloco de patches
        updated_context = await stage.run(context)

    # Verificações
    assert updated_context.report != ""
    assert "Resumo executivo simulado" in updated_context.report
    assert "Sentiment Markdown" in updated_context.report
    assert "Comparison Markdown" in updated_context.report

    # Verifica se chamou complete de forma consolidada
    mock_llm.complete.assert_called_once()


def _make_report_stage_with_mocks():
    """Cria um ReportStage com LLM/cache mockados para os testes de auditoria."""
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(
        return_value="""{
        "executive_summary": "Resumo executivo simulado com dados concretos e válidos para ultrapassar os cinquenta caracteres exigidos pelo validador.",
        "recommendation": "Recomendação simulada principal e alternativas estratégicas com detalhes adicionais do projeto para passar a regra de comprimento do validador.",
        "trends": "Tendências tecnológicas simuladas com evidência concreta de mercado e adoção de comunidade de desenvolvedores ativos."
    }"""
    )
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    return ReportStage(llm_client=mock_llm, cache=mock_cache)


def _make_context_with_results():
    from datetime import datetime

    results = [
        SynthesizedResult(
            entity="entity_a",
            title="Project A",
            description="A great project",
            sources=["github"],
            combined_score=0.9,
            highlights=["fast"],
            metrics={"stars": 1000},
        )
    ]
    metadata = ResearchMetadata(
        query="test query",
        timestamp=datetime.now(),
        domain="general",
        sources=["github"],
        total_results=1,
        iterations=1,
        overall_confidence=0.95,
        low_confidence_warnings=[],
    )
    context = PipelineContext(query="test query")
    context.synthesized_results = results
    context.metadata = metadata
    return context


@pytest.mark.asyncio
async def test_report_stage_calls_auditor_when_enable_auditor_true():
    """§14.1 — modo com enable_auditor=True deve chamar orchestrator.auditor.audit()."""
    stage = _make_report_stage_with_mocks()

    audit_report = MagicMock()
    audit_report.enriched_content = ""
    mock_orchestrator = MagicMock()
    mock_orchestrator.auditor.audit = AsyncMock(return_value=audit_report)
    mock_orchestrator.operation_mode.enable_auditor = True

    context = _make_context_with_results()
    context.extras["orchestrator"] = mock_orchestrator

    with patch("src.temporal_analyzer.TemporalAnalyzer"), patch(
        "src.sentiment_analyzer.SentimentAnalyzer"
    ), patch("src.comparator.Comparator"):
        await stage.run(context)

    mock_orchestrator.auditor.audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_stage_skips_auditor_when_enable_auditor_false():
    """§14.1 — modo com enable_auditor=False (ex.: guerrilha) não chama o auditor."""
    stage = _make_report_stage_with_mocks()

    mock_orchestrator = MagicMock()
    mock_orchestrator.auditor.audit = AsyncMock()
    mock_orchestrator.operation_mode.enable_auditor = False

    context = _make_context_with_results()
    context.extras["orchestrator"] = mock_orchestrator

    with patch("src.temporal_analyzer.TemporalAnalyzer"), patch(
        "src.sentiment_analyzer.SentimentAnalyzer"
    ), patch("src.comparator.Comparator"):
        await stage.run(context)

    mock_orchestrator.auditor.audit.assert_not_awaited()
