"""
Testes para TruLens Integration — Validação de avaliação contínua de agente.

Testa:
- Gravação de runs de avaliação
- Verificação de qualidade com thresholds
- Relatório agregado de qualidade
- Exportação para contexto do pipeline
- Avaliação de síntese
"""

import pytest

# Todos os testes neste módulo são de integração.
pytestmark = pytest.mark.integration

from unittest.mock import MagicMock, patch, AsyncMock

from src.evaluation.trulens_integration import (
    TruLensRecorder,
    QualityAppraiser,
    record_trulens_run,
    export_trulens_report,
)
from src.pipeline.pipeline import PipelineContext
from src.types import SearchResult


class TestTruLensRecorder:
    """Testes para TruLensRecorder."""

    def test_init_without_trulens(self):
        """Deve inicializar sem TruLens (usando mock)."""
        recorder = TruLensRecorder(enabled=True)

        assert recorder is not None
        # Pode ou não ter TruLens (depende do ambiente)
        assert hasattr(recorder, "_records")

    def test_record_stores_quality_record(self):
        """record deve armazenar QualityRecord."""
        recorder = TruLensRecorder(enabled=False)
        recorder.record("synthesize", metrics={"faithfulness": 0.9})

        assert len(recorder.get_records()) == 1
        assert recorder.get_records()[0].stage_name == "synthesize"

    def test_record_passed_flag(self):
        """record deve calcular flag 'passed' corretamente."""
        recorder = TruLensRecorder(enabled=False)

        # Métricas boas
        recorder.record("synthesize", metrics={"faithfulness": 0.9, "answer_relevance": 0.8})
        assert recorder.get_records()[-1].passed is True

        # Métricas ruins
        recorder.record("synthesize", metrics={"faithfulness": 0.3, "answer_relevance": 0.4})
        assert recorder.get_records()[-1].passed is False

    def test_record_with_error(self):
        """record com erro deve marcar passed=False."""
        recorder = TruLensRecorder(enabled=False)
        recorder.record("search", error="Connection failed")

        assert recorder.get_records()[-1].passed is False
        assert recorder.get_records()[-1].error == "Connection failed"

    def test_alert_callback_invoked(self):
        """Callback de alerta deve ser chamado em qualidade baixa."""
        alert_calls = []

        def alert_callback(stage_name, metrics):
            alert_calls.append((stage_name, metrics))

        recorder = TruLensRecorder(enabled=False, alert_callback=alert_callback)
        recorder.record("synthesize", metrics={"faithfulness": 0.2})

        assert len(alert_calls) == 1
        assert alert_calls[0][0] == "synthesize"

    def test_export_to_context(self):
        """export_to_context deve adicionar registros ao contexto."""
        recorder = TruLensRecorder(enabled=False)
        recorder.record("synthesize", metrics={"faithfulness": 0.9})

        context = PipelineContext(query="test")
        context = recorder.export_to_context(context)

        assert "trulens_records" in context.extra
        assert len(context.extra["trulens_records"]) == 1


class TestQualityAppraiser:
    """Testes para QualityAppraiser."""

    @pytest.fixture
    def appraiser(self):
        return QualityAppraiser(TruLensRecorder(enabled=False))

    @pytest.mark.asyncio
    async def test_appraise_synthesis(self, appraiser):
        """Avaliação de síntese deve retornar métricas de qualidade."""
        context = PipelineContext(query="test query")

        metrics = await appraiser.appraise_synthesis(
            context,
            synthesized="This is a good answer about test query",
            sources=[SearchResult(source="web", title="Test")],
        )

        assert "faithfulness" in metrics
        assert "answer_relevance" in metrics
        assert "context_recall" in metrics
        assert 0.0 <= metrics["faithfulness"] <= 1.0

async def test_compare_runs(self, appraiser):
        """Comparação de runs deve identificar vencedor."""
        result = await appraiser.compare_runs(
            {"overall_score": 0.8},
            {"overall_score": 0.6},
        )

        assert result["winner"] == "A"
        assert abs(result["improvement"] - 0.2) < 0.01  # Floating point comparison


class TestRecordHook:
    """Testes para hook record_trulens_run."""

    @pytest.mark.asyncio
    async def test_record_trulens_run(self):
        """Hook deve registrar run no recorder e exportar para contexto."""
        recorder = TruLensRecorder(enabled=False)
        context = PipelineContext(query="test")

        record_trulens_run(recorder, "search", context, metrics={"relevance": 0.8})

        assert "trulens_records" in context.extra
        assert recorder.get_records()[0].stage_name == "search"


class TestExportReport:
    """Testes para export_trulens_report."""

    def test_export_empty_recorder(self):
        """Export de recorder vazio deve retornar estrutura vazia."""
        recorder = TruLensRecorder(enabled=False)

        report = export_trulens_report(recorder)

        assert report["total_evaluations"] == 0
        assert report["pass_rate"] == 1.0

    def test_export_with_records(self):
        """Export com registros deve calcular métricas agregadas."""
        recorder = TruLensRecorder(enabled=False)
        recorder.record("synthesize", metrics={"faithfulness": 0.9})
        recorder.record("search", metrics={"answer_relevance": 0.8})

        report = export_trulens_report(recorder)

        assert report["total_evaluations"] == 2
        assert report["passed_evaluations"] == 2
        assert report["pass_rate"] == 1.0
        assert "faithfulness" in report["metrics_by_stage"]
        assert "answer_relevance" in report["metrics_by_stage"]
