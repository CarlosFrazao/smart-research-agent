"""
Testes para RAGAS Integration — Validação de avaliação contínua.

Testa:
- Avaliação básica de resposta
- Avaliação por etapa do pipeline
- Armazenamento de métricas no contexto
- Validação de qualidade com threshold
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from src.evaluation.ragas_integration import (
    RagasEvaluator,
    StageEvaluator,
    ragas_pipeline,
    store_eval_metrics,
    validate_ragas_quality,
)
from src.pipeline.pipeline import PipelineContext
from src.types import SearchResult


class TestRagasEvaluator:
    """Testes para RagasEvaluator."""

    @pytest.mark.asyncio
    async def test_evaluate_disabled_returns_empty(self):
        """Quando desativado, deve retornar dicionário vazio."""
        evaluator = RagasEvaluator(enabled=False)
        context = PipelineContext(query="test")

        metrics = await evaluator.evaluate(context)

        assert metrics == {}

    @pytest.mark.asyncio
    async def test_evaluate_returns_metrics_dict(self):
        """Avaliação deve retornar dicionário com métricas esperadas."""
        evaluator = RagasEvaluator(enabled=True)
        context = PipelineContext(query="test", report="Generated report content")

        metrics = await evaluator.evaluate(context)

        assert isinstance(metrics, dict)
        # Quando RAGAS não está disponível, retorna métricas sintéticas (mock)
        # Métricas padrão (com fallback sintético)
        assert "answer_relevance" in metrics or "faithfulness" in metrics or "source_precision" in metrics or "overall_score" in metrics


class TestStageEvaluator:
    """Testes para StageEvaluator."""

    @pytest.fixture
    def evaluator(self):
        return StageEvaluator(RagasEvaluator(enabled=True))

    def test_generate_stage_metrics_search(self, evaluator):
        """Métricas para etapa 'search' devem conter campos esperados."""
        context = PipelineContext(query="test")
        context.raw_results = [SearchResult(source="web", title="test")]

        metrics = evaluator._generate_stage_metrics("search", context)

        assert "relevance_raw" in metrics
        assert "coverage" in metrics
        assert metrics["metrics_source"] == "search"

    def test_generate_stage_metrics_synthesize(self, evaluator):
        """Métricas para etapa 'synthesize' devem conter campos esperados."""
        context = PipelineContext(query="test")
        context.extra["citations"] = ["url1", "url2"]

        metrics = evaluator._generate_stage_metrics("synthesize", context)

        assert "narrative_coherence" in metrics
        assert "internal_citations" in metrics
        assert metrics["internal_citations"] == 2

    def test_generate_stage_metrics_verification(self, evaluator):
        """Métricas para etapa 'verification' devem calcular taxa de verificação."""
        context = PipelineContext(query="test")
        context.extra["verified_claims"] = [
            {"status": "verified"},
            {"status": "verified"},
            {"status": "failed"},
        ]

        metrics = evaluator._generate_stage_metrics("verification", context)

        assert "claim_verification_rate" in metrics
        assert metrics["claim_verification_rate"] == 2 / 3


class TestRagasPipelineHook:
    """Testes para hook ragas_pipeline."""

    @pytest.mark.asyncio
    async def test_ragas_pipeline_stores_metrics(self):
        """ragas_pipeline deve armazenar métricas no contexto."""
        evaluator = RagasEvaluator(enabled=True)
        context = PipelineContext(query="test", report="Content")

        updated_context = await ragas_pipeline(context, evaluator, "synthesize")

        assert "ragas_metrics" in updated_context.extra
        assert "synthesize" in updated_context.extra["ragas_metrics"]


class TestStoreEvalMetrics:
    """Testes para store_eval_metrics."""

    @pytest.mark.asyncio
    async def test_store_eval_metrics_merges_correctly(self):
        """store_eval_metrics deve mesclar métricas sem sobrescrever."""
        context = PipelineContext(query="test")
        context.extra["ragas_metrics"] = {"existing": "value"}

        updated = await store_eval_metrics(context, {"new": "metric"})

        assert updated.extra["ragas_metrics"]["existing"] == "value"
        assert updated.extra["ragas_metrics"]["new"] == "metric"


class TestValidateRagasQuality:
    """Testes para validate_ragas_quality."""

    @pytest.mark.asyncio
    async def test_validate_passing_metrics(self):
        """Deve retornar True quando métricas passam o threshold."""
        context = PipelineContext(query="test")
        context.extra["ragas_metrics"] = {
            "synthesize": {
                "overall_score": 0.85,
                "faithfulness": 0.90,
                "answer_relevance": 0.80,
            }
        }

        result = await validate_ragas_quality(context, threshold=0.6)

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_failing_metrics(self):
        """Deve retornar False quando métricas falham o threshold."""
        context = PipelineContext(query="test")
        context.extra["ragas_metrics"] = {
            "synthesize": {
                "overall_score": 0.40,
                "faithfulness": 0.35,
                "answer_relevance": 0.50,
            }
        }

        result = await validate_ragas_quality(context, threshold=0.6)

        assert result is False

    @pytest.mark.asyncio
    async def test_validate_stage_filter(self):
        """Deve validar apenas estágios especificados no filtro."""
        context = PipelineContext(query="test")
        context.extra["ragas_metrics"] = {
            "synthesize": {"overall_score": 0.80},
            "verification": {"overall_score": 0.30},
        }

        result = await validate_ragas_quality(
            context,
            threshold=0.6,
            stage_filter=["verification"],
        )

        assert result is False  # verification falhou, deve ser considerado

        result = await validate_ragas_quality(
            context,
            threshold=0.6,
            stage_filter=["synthesize"],
        )

        assert result is True  # synthesis passou