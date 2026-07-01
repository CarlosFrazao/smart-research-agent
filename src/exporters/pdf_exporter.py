"""
pdf_exporter.py — Exportador de relatórios para PDF via reportlab.

Dependência opcional: pip install reportlab
Se reportlab não estiver instalado, o exporter emite um aviso e retorna None.
"""
import logging
import textwrap
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pdf_exporter")

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    logger.warning(
        "PDFExporter: reportlab não está instalado. "
        "Execute `pip install reportlab` para habilitar exportação PDF."
    )


class PDFExporter:
    """
    Converte um relatório Markdown (string) em um arquivo PDF estruturado usando reportlab.
    Opera em modo degradado (retorna None com warning) se reportlab não estiver disponível.
    """

    def __init__(self):
        self.available = _REPORTLAB_AVAILABLE

    def export(self, markdown_content: str, filepath: str) -> Optional[str]:
        """
        Gera o PDF a partir do conteúdo Markdown e o salva em `filepath`.

        Returns:
            Caminho do arquivo gerado, ou None se reportlab não disponível.
        """
        if not self.available:
            logger.warning("PDFExporter: exportação ignorada — reportlab não disponível.")
            return None

        pdf_path = str(filepath).replace(".md", ".pdf")
        Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A4,
            leftMargin=2.5 * cm,
            rightMargin=2.5 * cm,
            topMargin=2.5 * cm,
            bottomMargin=2.5 * cm,
        )

        styles = getSampleStyleSheet()
        h1_style = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=18, spaceAfter=12)
        h2_style = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=14, spaceAfter=8)
        h3_style = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=12, spaceAfter=6)
        body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14)
        blockquote_style = ParagraphStyle(
            "Blockquote", parent=styles["Normal"], fontSize=10,
            leftIndent=20, textColor=colors.HexColor("#555555"), leading=14
        )

        story = []
        for line in markdown_content.splitlines():
            stripped = line.strip()
            if not stripped:
                story.append(Spacer(1, 6))
                continue

            if stripped.startswith("# "):
                story.append(Paragraph(self._clean_md(stripped[2:]), h1_style))
            elif stripped.startswith("## "):
                story.append(Paragraph(self._clean_md(stripped[3:]), h2_style))
            elif stripped.startswith("### "):
                story.append(Paragraph(self._clean_md(stripped[4:]), h3_style))
            elif stripped.startswith("---"):
                story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC")))
                story.append(Spacer(1, 6))
            elif stripped.startswith("> "):
                story.append(Paragraph(self._clean_md(stripped[2:]), blockquote_style))
            else:
                # Texto comum (inclui listas)
                story.append(Paragraph(self._clean_md(stripped), body_style))

        try:
            doc.build(story)
            logger.info(f"PDFExporter: PDF gerado em {pdf_path}")
            return pdf_path
        except Exception as e:
            logger.error(f"PDFExporter: falha ao gerar PDF: {e}")
            return None

    def _clean_md(self, text: str) -> str:
        """
        Converte marcação Markdown básica em HTML simples compatível com reportlab Paragraph.
        """
        import re
        # Bold **text**
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        # Italic *text*
        text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
        # Inline code `text`
        text = re.sub(r"`(.+?)`", r"<font name='Courier'>\1</font>", text)
        # Links [text](url)
        text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"\1 (\2)", text)
        # Emojis e caracteres especiais que podem quebrar o reportlab — remove
        text = re.sub(r"[^\x00-\x7F]+", lambda m: m.group().encode("ascii", "ignore").decode(), text)
        return text
