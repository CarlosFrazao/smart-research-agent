"""
bibtex_exporter.py — Exportador de referências acadêmicas no formato BibTeX.

Suporta:
  - Artigos acadêmicos (arXiv, Semantic Scholar, PubMed)
  - Sites técnicos (Medium, Dev.to, Hacker News)
  - Repositórios de código (GitHub)

Formato compatível com Zotero, Mendeley, JabRef.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger("bibtex_exporter")


@dataclass
class BibTeXEntry:
    """Uma entrada BibTeX para exportação."""

    key: str  # Cite key (ex: author2024title)
    entry_type: str  # article, book, inproceedings, techreport, web, etc.
    fields: dict[str, str]  # title, author, year, journal, url, etc.

    def to_bibtex(self) -> str:
        """Converte a entrada para formato BibTeX."""
        lines = [f"@{self.entry_type}{{{self.key},"]
        for key, value in self.fields.items():
            if value:
                lines.append(f"  {key} = {{{value}}},")
        lines.append("}")
        return "\n".join(lines)


class BibTeXExporter:
    """Exportador de referências para formato BibTeX."""

    @staticmethod
    def from_search_result(
        result: dict[str, Any], index: int = 0
    ) -> BibTeXEntry | None:
        """
        Cria uma entrada BibTeX a partir de um SearchResult.

        Args:
            result: Dicionário com campos title, authors, year, url, etc.
            index: Índice para gerar cite key único se necessário.

        Returns:
            BibTeXEntry ou None se dados insuficientes.
        """
        title = result.get("title", "")
        authors = result.get("authors", [])
        url = result.get("url", "")
        year = result.get("year", datetime.now().year)
        source = result.get("source", "web")

        if not title:
            return None

        # Normaliza autor(es)
        author_str = BibTeXExporter._format_authors(authors)

        # Determina o tipo de entrada
        entry_type = BibTeXExporter._determine_entry_type(source, result)

        # Gera cite key
        key = BibTeXExporter._generate_key(title, author_str, year, index)

        # Campos comuns
        fields: dict[str, str] = {
            "title": title,
            "author": author_str,
            "year": str(year),
            "url": url,
        }

        # Campos específicos por tipo
        if entry_type == "article":
            if journal := result.get("journal"):
                fields["journal"] = journal
            if volume := result.get("volume"):
                fields["volume"] = volume
            if pages := result.get("pages"):
                fields["pages"] = pages
        elif entry_type == "inproceedings":
            if booktitle := result.get("booktitle"):
                fields["booktitle"] = booktitle

        fields["howpublished"] = f"\\url{{{url}}}" if url else ""

        return BibTeXEntry(key=key, entry_type=entry_type, fields=fields)

    @staticmethod
    def export_batch(results: list[dict[str, Any]], filename: str | None = None) -> str:
        """
        Exporta múltiplas referências para um único arquivo .bib.

        Args:
            results: Lista de dicionários de resultados de pesquisa.
            filename: Nome do arquivo opcional (se None, retorna string).

        Returns:
            Conteúdo completo do arquivo .bib.
        """
        entries: list[str] = [
            "% BibTeX export - Smart Research Agent",
            f"% Generated: {datetime.now().isoformat()}",
            "",
        ]

        for idx, result in enumerate(results):
            entry = BibTeXExporter.from_search_result(result, idx)
            if entry:
                entries.append(entry.to_bibtex())
                entries.append("")

        content = "\n".join(entries).strip() + "\n"

        if filename:
            Path(filename).write_text(content, encoding="utf-8")
            logger.info(
                "BibTeXExporter: exportado %d entradas para %s", len(entries), filename
            )

        return content

    # ── Helpers internos ──────────────────────────────────────────────────────

    @staticmethod
    def _format_authors(authors: list[str] | str) -> str:
        """Formata lista de autores para estilo BibTeX (sobrenome, nome)."""
        if isinstance(authors, str):
            # Tenta extrair autores de uma string JSON; se não for JSON válido,
            # trata a string inteira como um único autor.
            raw = authors
            try:
                parsed = json.loads(raw)
                authors = parsed if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                authors = [raw]

        if not authors:
            return "Anonymous"

        formatted = []
        for author in authors[:10]:  # Limite razoável
            # Tenta separar nome completo em sobrenome + nome
            parts = re.split(r"\s+", author.strip())
            if len(parts) >= 2:
                formatted.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            else:
                formatted.append(author)

        return " and ".join(formatted)

    @staticmethod
    def _determine_entry_type(source: str, result: dict[str, Any]) -> str:
        """Determina o tipo BibTeX apropriado baseado na fonte."""
        source_lower = source.lower()
        if "arxiv" in source_lower:
            return "article"
        if "github" in source_lower:
            return "web"
        if "pubmed" in source_lower:
            return "article"
        if "semantic_scholar" in source_lower or "scholar" in source_lower:
            return "article"
        if "conference" in result.get("title", "").lower():
            return "inproceedings"
        if result.get("journal"):
            return "article"
        return "web"

    @staticmethod
    def _generate_key(title: str, author: str, year: int, index: int) -> str:
        """Gera uma cite key única e legível."""
        # Usa primeira palavra do título + sobrenome do primeiro autor + ano
        title_word = re.sub(r"[^a-zA-Z0-9]", "", title.split()[0].lower())[:8]
        author_last = ""
        if author and author != "Anonymous":
            parts = author.split()
            if parts:
                author_last = re.sub(r"[^a-zA-Z0-9]", "", parts[0].lower())[:8]
        key = f"{author_last}{year}{title_word}{index}"
        return re.sub(r"[^a-zA-Z0-9]", "", key) or f"entry{index}"


# Import json for _format_authors
import json
from pathlib import Path
