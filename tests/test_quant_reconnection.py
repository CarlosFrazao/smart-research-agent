"""Testes de reconexão da análise quantitativa (Fase 6.4).

Valida que QuantAnalysisStage religa o DataAnalyzer e só executa
quando há arquivos de dados + pergunta em `context.extra`,
gravando `context.extra['quant_analysis']`. É não-crítico.
"""

import pytest
from unittest.mock import MagicMock

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.quant_analysis_stage import QuantAnalysisStage


class _FakeResult:
    def __init__(self):
        self.status = "success"

    def to_dict(self):
        return {
            "question": "q",
            "status": "success",
            "stdout": "OUT",
            "files_analyzed": ["a.csv"],
        }


@pytest.fixture
def analyzer():
    a = MagicMock()
    a.analyze = MagicMock(return_value=_FakeResult())
    return a


@pytest.mark.asyncio
async def test_runs_with_data_and_question(analyzer):
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    ctx = PipelineContext(query="q")
    ctx.extra["data_files"] = ["a.csv"]
    ctx.extra["data_question"] = "market share?"
    await stage.run(ctx)

    assert ctx.extra["quant_analysis"] is not None
    assert ctx.extra["quant_analysis"]["status"] == "success"
    analyzer.analyze.assert_called_once()


@pytest.mark.asyncio
async def test_skips_without_data(analyzer):
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    ctx = PipelineContext(query="q")
    await stage.run(ctx)
    assert ctx.extra["quant_analysis"] is None
    analyzer.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_skips_without_question(analyzer):
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    ctx = PipelineContext(query="q")
    ctx.extra["data_files"] = ["a.csv"]
    await stage.run(ctx)
    assert ctx.extra["quant_analysis"] is None


@pytest.mark.asyncio
async def test_passes_timeout(analyzer):
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    ctx = PipelineContext(query="q")
    ctx.extra["data_files"] = ["a.csv"]
    ctx.extra["data_question"] = "q?"
    ctx.extra["data_timeout"] = 12.0
    await stage.run(ctx)
    _, kwargs = analyzer.analyze.call_args
    assert kwargs.get("timeout") == 12.0


@pytest.mark.asyncio
async def test_stage_is_non_critical(analyzer):
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    assert stage.critical is False


@pytest.mark.asyncio
async def test_failure_is_non_fatal(analyzer):
    analyzer.analyze = MagicMock(side_effect=RuntimeError("boom"))
    stage = QuantAnalysisStage(data_analyzer=analyzer)
    ctx = PipelineContext(query="q")
    ctx.extra["data_files"] = ["a.csv"]
    ctx.extra["data_question"] = "q?"
    # Não deve levantar exceção.
    await stage.run(ctx)
    assert ctx.extra["quant_analysis"]["status"] == "error"
