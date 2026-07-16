"""
test_session_index — Testes TDD do SessionSearchIndex (FEAT-001).

Cobertura:
- 3 inserções (index) funcionam e retornam ids distintos.
- Busca por termo existente retorna hit (search hit).
- Busca por termo inexistente retorna lista vazia (search miss).
- Fallback LIKE forçado (sem FTS5) também encontra o termo.
- Validação de path traversal rejeita caminhos fora da raiz.
- Redaction: query com secret não persiste o secret em claro.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.memory.session_index import SessionSearchIndex, _validate_report_path


def _make_index(allowed_root, tmp_path):
    """Helper: cria índice em arquivo temporário com raiz permitida."""
    db = str(tmp_path / "idx.db")
    return SessionSearchIndex(db, allowed_root=allowed_root)


def test_index_three_inserts_returns_distinct_ids(tmp_path):
    """Três index() retornam ids distintos e persistem."""
    root = tmp_path
    idx = _make_index(root, tmp_path)
    p1 = str(tmp_path / "r1.md")
    p2 = str(tmp_path / "r2.md")
    p3 = str(tmp_path / "r3.md")
    id1 = idx.index("concorrência em grafos de conhecimento", p1)
    id2 = idx.index("roteamento de LLMs locais", p2)
    id3 = idx.index("busca híbrida vetorial e lexical", p3)
    assert id1 != id2 != id3
    assert len(idx.search("concorrência", limit=10)) == 1
    idx.close()


def test_search_hit_finds_existing_term(tmp_path):
    """Busca por termo presente retorna o registro correto."""
    root = tmp_path
    idx = _make_index(root, tmp_path)
    idx.index("agente de pesquisa autônomo", str(tmp_path / "a.md"))
    hits = idx.search("agente", limit=5)
    assert len(hits) == 1
    assert "agente" in hits[0]["query"]
    assert hits[0]["report_path"].endswith("a.md")
    idx.close()


def test_search_miss_returns_empty(tmp_path):
    """Busca por termo ausente retorna lista vazia."""
    root = tmp_path
    idx = _make_index(root, tmp_path)
    idx.index("machine learning explicável", str(tmp_path / "b.md"))
    assert idx.search("criptografia quântica", limit=5) == []
    # Termo vazio também não quebra.
    assert idx.search("", limit=5) == []
    idx.close()


def test_forced_like_fallback_finds_term(tmp_path, monkeypatch):
    """Quando FTS5 indisponível, o fallback LIKE ainda encontra o termo."""
    root = tmp_path
    db = str(tmp_path / "idx_like.db")
    idx = SessionSearchIndex(db, allowed_root=root)
    # Força o backend para LIKE simulando ausência de FTS5.
    idx.uses_fts5 = False
    idx.index("resumo de artigos científicos", str(tmp_path / "c.md"))
    hits = idx.search("artigos", limit=5)
    assert len(hits) == 1
    assert "artigos" in hits[0]["query"]
    idx.close()


def test_path_traversal_rejected(tmp_path):
    """report_path com '..' saindo da raiz é rejeitado."""
    root = tmp_path
    with pytest.raises(ValueError):
        _validate_report_path("../escape.md", root)
    # Caminho absoluto fora da raiz também é rejeitado.
    outside = (tmp_path.parent / "outside.md").resolve()
    with pytest.raises(ValueError):
        _validate_report_path(str(outside), root)


def test_index_accepts_relative_path_inside_root(tmp_path):
    """Caminho relativo dentro da raiz é aceito e normalizado."""
    root = tmp_path
    safe = _validate_report_path("reports/out.md", root)
    assert safe.is_absolute()
    assert str(safe).startswith(str(root.resolve()))


def test_redaction_applied_before_persist(tmp_path):
    """Query contendo secret não grava o secret em claro no índice."""
    root = tmp_path
    db = str(tmp_path / "idx_red.db")
    idx = SessionSearchIndex(db, allowed_root=root)
    idx.index("relatório com sk-ant-abcdefghij123456 inside", str(tmp_path / "d.md"))
    # Inspeciona direto no SQLite: o secret não deve estar em claro.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT query FROM sessions").fetchone()
    conn.close()
    assert "sk-ant-abcdefghij123456" not in row["query"]
    assert "sk-ant" in row["query"]  # prefixo preservado pela mask
    idx.close()
