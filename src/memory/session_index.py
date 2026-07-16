"""
session_index — Índice de busca textual (FTS5) de sessões de pesquisa do SRA.

Este módulo cria um índice local, baseado em SQLite, que permite encontrar
sessões de pesquisa anteriores a partir de termos da query ou do conteúdo do
relatório.  É usado pelo ``GapDetector`` (FEAT-002) para enriquecer a detecção
de lacunas com contexto de sessões passadas.

Características:
- Usa SQLite FTS5 quando a extensão está compilada (caso normal em Python 3.11+).
- Faz fallback gracioso para busca ``LIKE`` quando o FTS5 está indisponível
  (ex.: build do Python sem a extensão, ou ambiente restrito).
- Aplica ``redact_sensitive_text`` (de ``src.logging_utils``) na query antes de
  persistir, garantindo que segredos nunca sejam gravados no índice.
- Valida ``report_path`` contra path traversal (``..`` e caminhos absolutos
  fora de uma raiz permitida) antes de aceitar o registro.

O módulo é self-contained: importa apenas stdlib + ``logging_utils`` (que por
sua vez só usa stdlib + dependência opcional no Windows).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from src.logging_utils import redact_sensitive_text

logger = logging.getLogger(__name__)

# Raiz permitida para ``report_path``.  O índice só aceita caminhos que,
# após normalização, permaneçam dentro desta raiz.  Por padrão usamos o
# diretório de trabalho atual; pode ser sobrescrito por quem instancia a classe
# (ex.: o diretório de ``reports/`` do SRA).
_DEFAULT_ALLOWED_ROOT = Path.cwd()


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Retorna True se o SQLite desta conexão suporta a extensão FTS5.

    Consulta ``PRAGMA compile_options`` e procura pela opção ``ENABLE_FTS5``.
    Em caso de erro na PRAGMA, retorna False (degradação segura para LIKE).
    """
    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - caminho de erro de PRAGMA
        logger.warning("PRAGMA compile_options falhou: %s — usando fallback LIKE", exc)
        return False
    for (option,) in rows:
        if option == "ENABLE_FTS5":
            return True
    return False


def _validate_report_path(report_path: str, allowed_root: Path) -> Path:
    """Valida e normaliza ``report_path`` contra path traversal.

    Regras:
    - Não pode conter segmentos ``..`` resolvidos para fora da raiz.
    - Deve residir dentro de ``allowed_root`` após ``resolve()``.

    Args:
        report_path: Caminho (absoluto ou relativo) do relatório.
        allowed_root: Raiz permitida para o caminho.

    Returns:
        ``Path`` normalizado e seguro.

    Raises:
        ValueError: Se o caminho tenta sair da raiz permitida (path traversal).
    """
    if not report_path or not report_path.strip():
        raise ValueError("report_path vazio não é permitido")

    candidate = Path(report_path)
    # Normaliza relativos para a raiz permitida antes de resolver.
    if not candidate.is_absolute():
        candidate = (allowed_root / candidate).resolve()
    else:
        candidate = candidate.resolve()

    root_resolved = allowed_root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        # Está fora da raiz permitida → path traversal.
        raise ValueError(
            f"report_path fora da raiz permitida (path traversal): {report_path!r}"
        ) from None
    return candidate


class SessionSearchIndex:
    """Índice de sessões de pesquisa com busca textual (FTS5 + fallback LIKE).

    Armazena pares (query, report_path) e permite buscar sessões por termo.
    A query é redactada antes da persistência para nunca gravar segredos.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        allowed_root: Optional[Path] = None,
    ) -> None:
        """Inicializa o índice.

        Args:
            db_path: Caminho do arquivo SQLite, ou ``:memory:`` para um índice
                efêmero (usado nos testes).
            allowed_root: Raiz permitida para ``report_path``. Se None, usa o
                diretório de trabalho atual.
        """
        self.db_path = db_path
        self.allowed_root = (allowed_root or _DEFAULT_ALLOWED_ROOT).resolve()
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self.uses_fts5 = _fts5_available(self._conn)
        self._init_schema()

    def _init_schema(self) -> None:
        """Cria as tabelas/índices conforme o backend disponível (FTS5 ou LIKE)."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                report_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        if self.uses_fts5:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
                USING fts5(query, report_path, content='sessions',
                           content_rowid='id')
                """
            )
        else:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_query ON sessions(query)"
            )
        self._conn.commit()

    def index(self, query: str, report_path: str) -> int:
        """Indexa uma sessão de pesquisa.

        Aplica redaction na query antes de persistir e valida ``report_path``.

        Args:
            query: Texto da query da sessão (será redactado antes de gravar).
            report_path: Caminho do relatório associado (validado contra
                path traversal).

        Returns:
            O ``id`` da sessão recém-criada.

        Raises:
            ValueError: Se ``report_path`` for inválido (path traversal/vazio).
        """
        if query is None:
            query = ""
        safe_path = _validate_report_path(report_path, self.allowed_root)
        # Redacta a query para nunca persistir segredos no índice.
        safe_query = redact_sensitive_text(query, force=True)

        cur = self._conn.execute(
            "INSERT INTO sessions (query, report_path) VALUES (?, ?)",
            (safe_query, str(safe_path)),
        )
        row_id = int(cur.lastrowid)
        if self.uses_fts5:
            self._conn.execute(
                "INSERT INTO sessions_fts (rowid, query, report_path) VALUES (?, ?, ?)",
                (row_id, safe_query, str(safe_path)),
            )
        self._conn.commit()
        return row_id

    def search(self, term: str, limit: int = 10) -> list[dict]:
        """Busca sessões por termo.

        Args:
            term: Termo de busca (case-insensitive).
            limit: Número máximo de resultados.

        Returns:
            Lista de dicts ``{"id", "query", "report_path", "created_at"}``
            ordenada por relevância (FTS5 rank) ou por id decrescente (LIKE).
            Lista vazia se não houver hits ou ``term`` for vazio.
        """
        if not term or not term.strip():
            return []
        like_pattern = f"%{term}%"
        if self.uses_fts5:
            rows = self._conn.execute(
                """
                SELECT s.id, s.query, s.report_path, s.created_at
                FROM sessions_fts f
                JOIN sessions s ON s.id = f.rowid
                WHERE sessions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (term, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, query, report_path, created_at
                FROM sessions
                WHERE query LIKE ? OR report_path LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (like_pattern, like_pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        """Fecha a conexão SQLite liberando recursos."""
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - erro de close
            logger.warning("Erro ao fechar SessionSearchIndex: %s", exc)

    def __enter__(self) -> "SessionSearchIndex":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
