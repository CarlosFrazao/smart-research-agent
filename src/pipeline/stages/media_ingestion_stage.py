"""
media_ingestion_stage.py — Estágio de ingestão de mídia rica (Fase 6.3).

Religa os extratores órfãos do SRA ao pipeline de pesquisa:
  - ``PDFParser``      (``src/extractors/pdf_parser.py``)         → PDFs
  - ``OCRExtractor``   (``src/extractors/ocr_extractor.py``)    → Imagens
  - ``VideoTranscriber``(``src/extractors/video_transcriber.py``) → Vídeos
  - ``VisionAnalyzer``  (``src/vision_analyzer.py``)           → análise de diagramas/screenshots

Sempre que o pipeline (ou o ``ScrapingSearcher``) encontrar um arquivo
suportado (PDF, imagem, vídeo), este estágio roteia para o extrator
correspondente em vez de falhar ou extrair apenas metadados brutos.

O estágio é **não-crítico**: falhas de extração (dependência faltando,
arquivo corrompido) são logadas e não abortam o pipeline. Cada extrator
já possui fallback gracioso (retorna ``None``/``{}`` quando a dependência
opcional — pdfplumber, pytesseract, whisper, tesseract — não está
instalada), então o estágio nunca quebra silenciosamente.

Entradas (``context.extra["media_inputs"]``):
  Lista de ``str`` (caminho no disco) OU ``dict`` com chaves
  ``path`` (obrigatório), ``url`` (opcional), ``mime`` (opcional,
  usado para desambiguar imagem vs. pdf quando a extensão não basta).

Saída (``context.extra["media_extractions"]``):
  Lista de ``dict`` por arquivo processado:
    {
      "input": <path|url>,
      "kind": "pdf" | "image" | "video" | "unknown",
      "text": <texto extraído>,
      "tables": <list>,            # só PDF
      "references": <list>,        # só PDF
      "vision_analysis": <str>,     # só imagem com VisionAnalyzer
      "transcript": <str>,        # só vídeo
      "error": <str|None>,
    }
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("media-ingestion-stage")

# Extensões suportadas por tipo de mídia.
_PDF_EXTS = {".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


class MediaIngestionStage(PipelineStage):
    """Estágio não-crítico que extrai texto/dados de mídia rica."""

    name = "media_ingestion"
    critical = False

    def __init__(
        self,
        pdf_parser: Any | None = None,
        ocr_extractor: Any | None = None,
        video_transcriber: Any | None = None,
        vision_analyzer: Any | None = None,
        llm_client: Any = None,
        vision_fn: Any | None = None,
    ) -> None:
        """
        Args:
            pdf_parser: Instância de ``PDFParser`` (lazy-built se None).
            ocr_extractor: Instância de ``OCRExtractor`` (lazy-built se None).
            video_transcriber: Instância de ``VideoTranscriber`` (lazy se None).
            vision_analyzer: Instância de ``VisionAnalyzer`` já configurada
                com ``vision_fn``. Se None e ``llm_client`` fornecido,
                constrói um ``VisionAnalyzer`` a partir do cliente.
            llm_client: Cliente LLM opcional, usado para derivar o
                ``VisionAnalyzer`` quando ``vision_analyzer`` não é fornecido.
            vision_fn: ``(prompt, image_b64, mime) -> str`` injetável
                para o ``VisionAnalyzer`` (modo agnóstico de provider).
        """
        super().__init__()
        self._pdf_parser = pdf_parser
        self._ocr_extractor = ocr_extractor
        self._video_transcriber = video_transcriber
        self._vision_analyzer = vision_analyzer or self._build_vision_analyzer(
            llm_client, vision_fn
        )
        self._llm = llm_client

    # ── API pública ─────────────────────────────────────────────────────

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Processa as entradas de mídia e grava extrações em ``context.extra``."""
        inputs = self._collect_inputs(context)
        if not inputs:
            logger.info("MediaIngestionStage: nenhuma mídia para processar. Pulando.")
            context.extra["media_extractions"] = []
            return

        extractions: list[dict[str, Any]] = []
        for item in inputs:
            extraction = await self._process_one(item)
            extractions.append(extraction)
            logger.info(
                "MediaIngestionStage: %s (%s) processado.",
                extraction.get("input"),
                extraction.get("kind"),
            )

        context.extra["media_extractions"] = extractions
        logger.info(
            "MediaIngestionStage: %d arquivo(s) de mídia processado(s).",
            len(extractions),
        )
        return context

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _collect_inputs(context: PipelineContext) -> list[dict[str, Any]]:
        """Normaliza ``context.extra["media_inputs"]`` para ``dict`` por item."""
        raw = context.extra.get("media_inputs") or []
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                normalized.append({"path": item})
            elif isinstance(item, dict) and item.get("path"):
                normalized.append(item)
            else:
                logger.warning(
                    "MediaIngestionStage: entrada de mídia ignorada: %r", item
                )
        return normalized

    def _classify(self, item: dict[str, Any]) -> str:
        """Classifica o tipo de mídia por extensão ou mime."""
        path = item.get("path")
        mime = item.get("mime")
        if mime:
            if mime == "application/pdf":
                return "pdf"
            if mime.startswith("image/"):
                return "image"
            if mime.startswith("video/"):
                return "video"
        if path:
            ext = Path(path).suffix.lower()
            if ext in _PDF_EXTS:
                return "pdf"
            if ext in _IMAGE_EXTS:
                return "image"
            if ext in _VIDEO_EXTS:
                return "video"
        # Fallback por inferência de mime do próprio arquivo
        if path:
            guessed, _ = mimetypes.guess_type(path)
            if guessed == "application/pdf":
                return "pdf"
            if guessed and guessed.startswith("image/"):
                return "image"
            if guessed and guessed.startswith("video/"):
                return "video"
        return "unknown"

    async def _process_one(self, item: dict[str, Any]) -> dict[str, Any]:
        """Roteia um item para o extrator apropriado."""
        kind = self._classify(item)
        label = item.get("url") or item.get("path")
        base: dict[str, Any] = {
            "input": label,
            "kind": kind,
            "error": None,
        }
        try:
            if kind == "pdf":
                return {**base, **(await self._process_pdf(item))}
            if kind == "image":
                return {**base, **(await self._process_image(item))}
            if kind == "video":
                return {**base, **(await self._process_video(item))}
            logger.warning(
                "MediaIngestionStage: tipo de mídia desconhecido para %s", label
            )
            base["error"] = "unsupported media type"
            return base
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("MediaIngestionStage: falha ao processar %s: %s", label, e)
            base["error"] = str(e)
            return base

    async def _process_pdf(self, item: dict[str, Any]) -> dict[str, Any]:
        parser = self._pdf_parser or self._build_pdf_parser()
        if parser is None:
            return {
                "text": "",
                "tables": [],
                "references": [],
                "error": "pdfplumber not installed",
            }
        result = await parser.parse_file(item["path"])
        return {
            "text": result.get("text", ""),
            "tables": result.get("tables", []),
            "references": result.get("references", []),
            "error": result.get("error"),
        }

    async def _process_image(self, item: dict[str, Any]) -> dict[str, Any]:
        ocr = self._ocr_extractor or self._build_ocr_extractor()
        text = None
        if ocr is not None:
            text = await ocr.extract_from_file(item["path"])
        vision_analysis = None
        if self._vision_analyzer is not None:
            prompt = (
                "Extraia os eixos, rótulos e valores de qualquer gráfico, "
                "tabela ou diagrama de arquitetura presente nesta imagem. "
                "Se não houver, descreva brevemente o conteúdo relevante."
            )
            vision_analysis = await self._vision_analyzer.analyze_image(
                item["path"], prompt
            )
            if vision_analysis.startswith("VisionAnalyzer:"):
                vision_analysis = None
        return {
            "text": text or "",
            "vision_analysis": vision_analysis,
            "error": None if (text or vision_analysis) else "no OCR or vision output",
        }

    async def _process_video(self, item: dict[str, Any]) -> dict[str, Any]:
        transcriber = self._video_transcriber or self._build_video_transcriber()
        if transcriber is None:
            return {"transcript": None, "error": "whisper not installed"}
        transcript = await transcriber.transcribe_file(item["path"])
        return {
            "transcript": transcript,
            "error": None if transcript else "no transcript produced",
        }

    # ── Lazy builders (imports isolados, não quebram se faltar dep) ──

    def _build_pdf_parser(self) -> Any | None:
        try:
            from src.extractors.pdf_parser import PDFParser

            return PDFParser()
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("MediaIngestionStage: falha ao criar PDFParser: %s", e)
            return None

    def _build_ocr_extractor(self) -> Any | None:
        try:
            from src.extractors.ocr_extractor import OCRExtractor

            return OCRExtractor()
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("MediaIngestionStage: falha ao criar OCRExtractor: %s", e)
            return None

    def _build_video_transcriber(self) -> Any | None:
        try:
            from src.extractors.video_transcriber import VideoTranscriber

            return VideoTranscriber()
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning(
                "MediaIngestionStage: falha ao criar VideoTranscriber: %s", e
            )
            return None

    def _build_vision_analyzer(
        self, llm_client: Any, vision_fn: Any | None
    ) -> Any | None:
        try:
            from src.vision_analyzer import VisionAnalyzer

            return VisionAnalyzer(vision_fn=vision_fn, llm_client=llm_client)
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("MediaIngestionStage: falha ao criar VisionAnalyzer: %s", e)
            return None
