"""Testes de reconexão de mídia rica (Fase 6.3).

Valida que MediaIngestionStage roteia corretamente PDFs, imagens,
vídeos e tipos desconhecidos para os extratores órfãos
(PDFParser, OCRExtractor, VideoTranscriber, VisionAnalyzer),
gravando em `context.extra['media_extractions']`. O estágio é
não-crítico e nunca quebra silenciosamente.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.media_ingestion_stage import MediaIngestionStage


def _make_extractors():
    pdf = MagicMock()
    pdf.parse_file = AsyncMock(
        return_value={
            "text": "PDFTXT",
            "tables": [[1, 2]],
            "references": ["r1"],
            "pages": 1,
            "error": None,
        }
    )
    ocr = MagicMock()
    ocr.extract_from_file = AsyncMock(return_value="OCRTXT")
    vid = MagicMock()
    vid.transcribe_file = AsyncMock(return_value="TRANSC")
    vis = MagicMock()
    vis.analyze_image = AsyncMock(return_value="VISION")
    return pdf, ocr, vid, vis


@pytest.fixture
def stage():
    pdf, ocr, vid, vis = _make_extractors()
    return MediaIngestionStage(
        pdf_parser=pdf,
        ocr_extractor=ocr,
        video_transcriber=vid,
        vision_analyzer=vis,
    )


@pytest.mark.asyncio
async def test_routes_pdf(stage):
    ctx = PipelineContext(query="q")
    ctx.extra["media_inputs"] = ["/tmp/a.pdf"]
    await stage.run(ctx)
    ext = ctx.extra["media_extractions"][0]
    assert ext["kind"] == "pdf"
    assert ext["text"] == "PDFTXT"
    assert ext["references"] == ["r1"]


@pytest.mark.asyncio
async def test_routes_image_with_vision(stage):
    ctx = PipelineContext(query="q")
    ctx.extra["media_inputs"] = ["/tmp/b.png"]
    await stage.run(ctx)
    ext = ctx.extra["media_extractions"][0]
    assert ext["kind"] == "image"
    assert ext["text"] == "OCRTXT"
    assert ext["vision_analysis"] == "VISION"


@pytest.mark.asyncio
async def test_routes_video(stage):
    ctx = PipelineContext(query="q")
    ctx.extra["media_inputs"] = ["/tmp/c.mp4"]
    await stage.run(ctx)
    ext = ctx.extra["media_extractions"][0]
    assert ext["kind"] == "video"
    assert ext["transcript"] == "TRANSC"


@pytest.mark.asyncio
async def test_routes_by_mime_over_extension(stage):
    ctx = PipelineContext(query="q")
    ctx.extra["media_inputs"] = [{"path": "/tmp/d.weird", "mime": "application/pdf"}]
    await stage.run(ctx)
    ext = ctx.extra["media_extractions"][0]
    assert ext["kind"] == "pdf"
    assert ext["text"] == "PDFTXT"


@pytest.mark.asyncio
async def test_unknown_type_is_flagged(stage):
    ctx = PipelineContext(query="q")
    ctx.extra["media_inputs"] = ["/tmp/e.xyz"]
    await stage.run(ctx)
    ext = ctx.extra["media_extractions"][0]
    assert ext["kind"] == "unknown"
    assert ext["error"] == "unsupported media type"


@pytest.mark.asyncio
async def test_no_inputs_skips(stage):
    ctx = PipelineContext(query="q")
    await stage.run(ctx)
    assert ctx.extra["media_extractions"] == []


@pytest.mark.asyncio
async def test_stage_is_non_critical(stage):
    assert stage.critical is False


@pytest.mark.asyncio
async def test_lazy_build_without_injected_deps():
    """Sem extratores injetados, o estágio constrói-os lazy (gracioso)."""
    stage = MediaIngestionStage()
    ctx = PipelineContext(query="q")
    # Sem dependências reais => extração vazia mas sem crash.
    ctx.extra["media_inputs"] = [{"path": "/tmp/x.pdf", "mime": "application/pdf"}]
    await stage.run(ctx)
    assert "media_extractions" in ctx.extra
