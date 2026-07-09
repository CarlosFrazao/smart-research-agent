import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pipeline.stages.report_stage import ReportStage
from src.pipeline.pipeline import PipelineContext
from src.types import ResearchMetadata, SynthesizedResult

@pytest.mark.asyncio
async def test_report_stage_injects_sage_concorrencia():
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

    # Mock do orchestrator para simular o modo concorrencia
    mock_orchestrator = MagicMock()
    mock_orchestrator.llm = mock_llm
    
    mock_op_config = MagicMock()
    mock_op_config.name = "concorrencia"
    mock_op_config.cost_optimization = False
    mock_orchestrator.operation_config = mock_op_config

    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(return_value=None)
    mock_cache.set = AsyncMock(return_value=None)

    with patch("src.temporal_analyzer.TemporalAnalyzer", return_value=mock_temporal), \
         patch("src.sentiment_analyzer.SentimentAnalyzer", return_value=mock_sentiment), \
         patch("src.comparator.Comparator", return_value=mock_comparator), \
         patch("src.pipeline.stages.report_stage.AgentPersonaLoader") as MockLoaderClass:
         
        mock_loader = MockLoaderClass.return_value
        mock_loader.load.return_value = "# Sage Persona Content"
        mock_loader.build_enhanced_prompt.side_effect = lambda base, name: f"# Sage Persona Content\n\n---\n\n{base}"

        stage = ReportStage(llm_client=mock_llm, cache=mock_cache)
        stage.orchestrator = mock_orchestrator

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
    mock_loader.build_enhanced_prompt.assert_called_once()
    assert mock_loader.build_enhanced_prompt.call_args[0][1] == "sage_strategy"
