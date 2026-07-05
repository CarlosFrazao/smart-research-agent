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

    # Executa
    updated_context = await stage.run(context)

    # Verificações
    assert updated_context.report != ""
    assert "Resumo executivo simulado" in updated_context.report
    assert "Timeline Markdown" in updated_context.report
    assert "Sentiment Markdown" in updated_context.report
    assert "Comparison Markdown" in updated_context.report

    # Verifica se chamou complete de forma consolidada
    mock_llm.complete.assert_called_once()
