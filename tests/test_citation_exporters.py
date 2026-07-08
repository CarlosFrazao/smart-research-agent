"""
test_citation_exporters.py — Testes para BibTeX e RIS exporters.
"""

import pytest
from pathlib import Path

from src.exporters.bibtex_exporter import BibTeXExporter, BibTeXEntry
from src.exporters.ris_exporter import RISExporter, RISEntry


class TestBibTeXExporter:
    """Testes para o exportador BibTeX."""

    def test_basic_entry_generation(self) -> None:
        """Gera entrada básica quando dados são fornecidos."""
        result = {
            "title": "Async Patterns in Rust",
            "authors": ["Jane Doe", "John Smith"],
            "year": 2024,
            "url": "https://example.com/rust-async",
            "source": "github",
        }

        entry = BibTeXExporter.from_search_result(result)

        assert entry is not None
        assert entry.entry_type == "web"
        assert entry.key
        assert "Async Patterns in Rust" in entry.fields["title"]

    def test_arxiv_entry_type(self) -> None:
        """arXiv result gera entrada tipo 'article'."""
        result = {
            "title": "Attention Is All You Need",
            "authors": ["Ashish Vaswani"],
            "year": 2017,
            "url": "https://arxiv.org/abs/1706.03762",
            "source": "arxiv",
        }

        entry = BibTeXExporter.from_search_result(result)
        assert entry.entry_type == "article"

    def test_missing_title_returns_none(self) -> None:
        """Sem título, retorna None (não pode criar entrada válida)."""
        result = {"url": "https://example.com/no-title"}
        entry = BibTeXExporter.from_search_result(result)
        assert entry is None

    def test_batch_export(self, tmp_path: Path) -> None:
        """Exporta lote para string ou arquivo."""
        results = [
            {"title": "Paper 1", "authors": ["Author A"], "year": 2023, "url": "url1", "source": "web"},
            {"title": "Paper 2", "authors": ["Author B"], "year": 2024, "url": "url2", "source": "web"},
        ]

        content = BibTeXExporter.export_batch(results)

        assert "@web" in content
        assert "Paper 1" in content
        assert "Paper 2" in content

    def test_export_to_file(self, tmp_path: Path) -> None:
        """Exporta lote para arquivo .bib."""
        results = [{"title": "Article", "authors": [], "year": 2024, "url": "url", "source": "web"}]
        out_file = tmp_path / "references.bib"

        BibTeXExporter.export_batch(results, filename=str(out_file))

        assert out_file.exists()
        assert "@web" in out_file.read_text()


class TestRISExporter:
    """Testes para o exportador RIS."""

    def test_basic_entry_generation(self) -> None:
        """Gera entrada RIS básica quando dados são fornecidos."""
        result = {
            "title": "Python Performance",
            "authors": ["Alice"],
            "year": 2023,
            "url": "https://example.com/python",
            "source": "web",
        }

        entry = RISExporter.from_search_result(result)

        assert entry is not None
        assert entry.ref_type == "ELEC"
        assert "Python Performance" in entry.fields["TI"]

    def test_github_entry_type(self) -> None:
        """GitHub result gera entrada tipo ELEC (Electronic)."""
        result = {
            "title": "Cool Project",
            "authors": ["Dev"],
            "year": 2024,
            "url": "https://github.com/user/repo",
            "source": "github",
        }

        entry = RISExporter.from_search_result(result)
        assert entry.ref_type == "ELEC"

    def test_batch_export(self, tmp_path: Path) -> None:
        """Exporta lote RIS para string ou arquivo."""
        results = [
            {"title": "Entry 1", "authors": ["A"], "year": 2023, "url": "url1", "source": "web"},
            {"title": "Entry 2", "authors": ["B"], "year": 2024, "url": "url2", "source": "web"},
        ]

        content = RISExporter.export_batch(results)

        assert "TY  - ELEC" in content
        assert "Entry 1" in content
        assert "ER  -" in content

    def test_export_to_file(self, tmp_path: Path) -> None:
        """Exporta lote RIS para arquivo .ris."""
        results = [{"title": "Item", "authors": [], "year": 2024, "url": "url", "source": "web"}]
        out_file = tmp_path / "references.ris"

        RISExporter.export_batch(results, filename=str(out_file))

        assert out_file.exists()
        assert "TY  -" in out_file.read_text()
