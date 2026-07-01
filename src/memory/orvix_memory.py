"""
OrvixMemory — memória local SQLite para o smart-research-agent.

Portado do ORVIX-AI/dashboard/backend/knowledge/agent_memory.py com adaptações:
  - Nomes de classe desambiguados (MemoryEntry, MemorySearchResult)
  - Caminho do DB configurável via RESEARCH_MEMORY_DB (default: reports/.research_memory.db)
  - sentence-transformers opcional — sem ele, BM25 + grafo continuam funcionando
  - Método store_research_result() para persistir resumos de pesquisa com top-5 entidades

Arquitetura RRF:
  BM25 (SQLite FTS5) + Vector (sentence-transformers, opcional) + Grafo (entidades nomeadas)
  score_rrf = sum(1 / (60 + rank_i)) por modo ativo
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, List, Optional

logger = logging.getLogger(__name__)

_RRF_K = 60
_EMBED_MODEL = "all-MiniLM-L6-v2"
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b")

_DEFAULT_DB_PATH = Path(
    os.environ.get("RESEARCH_MEMORY_DB", "reports/.research_memory.db")
)


# ── Data contracts ────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    id: int
    content: str
    metadata: dict[str, Any]
    score: float = 0.0


@dataclass
class MemorySearchResult:
    entries: List[MemoryEntry] = field(default_factory=list)
    modes_used: List[str] = field(default_factory=list)


# ── Embedding helper (lazy-loaded) ────────────────────────────────────────────

_embedder_lock = threading.Lock()
_embedder_cache: Any = None


def _get_embedder() -> Any:
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache
    with _embedder_lock:
        if _embedder_cache is None:
            try:
                from sentence_transformers import SentenceTransformer
                _embedder_cache = SentenceTransformer(_EMBED_MODEL)
                logger.info("OrvixMemory: sentence-transformers carregado (vector search ativo)")
            except ImportError:
                logger.info("OrvixMemory: sentence-transformers não instalado — vector search desabilitado")
                _embedder_cache = None
    return _embedder_cache


def _embed(text: str) -> Optional[list]:
    model = _get_embedder()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True).tolist()


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── OrvixMemory ───────────────────────────────────────────────────────────────

class OrvixMemory:
    """Memória local SQLite com busca RRF (BM25 + Vector + Grafo)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = Path(db_path or str(_DEFAULT_DB_PATH))
        self._local = threading.local()
        self._init_schema()

    # ── Conexão ───────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                self._db_path, check_same_thread=False, timeout=10
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield self._local.conn
        except Exception:
            self._local.conn.rollback()
            raise

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    content    TEXT    NOT NULL,
                    embedding  TEXT,
                    metadata   TEXT    NOT NULL DEFAULT '{}',
                    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, content='memories', content_rowid='id');

                CREATE TABLE IF NOT EXISTS entities (
                    id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT    NOT NULL UNIQUE COLLATE NOCASE
                );

                CREATE TABLE IF NOT EXISTS entity_links (
                    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    PRIMARY KEY (memory_id, entity_id)
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content) VALUES (NEW.id, NEW.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES ('delete', OLD.id, OLD.content);
                END;
            """)
            conn.commit()

    # ── Escrita ───────────────────────────────────────────────────────────────

    def add(self, content: str, metadata: Optional[dict] = None) -> int:
        """Persiste um registro de memória e extrai entidades automaticamente."""
        meta = metadata or {}
        embedding = _embed(content)
        emb_json = json.dumps(embedding) if embedding else None

        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO memories (content, embedding, metadata) VALUES (?, ?, ?)",
                (content, emb_json, json.dumps(meta, default=str)),
            )
            memory_id: int = cur.lastrowid
            self._capture_entities(conn, memory_id, content)
            conn.commit()

        logger.debug("OrvixMemory: gravado id=%d", memory_id)
        return memory_id

    def _capture_entities(self, conn: sqlite3.Connection, memory_id: int, text: str) -> None:
        for name in set(_ENTITY_RE.findall(text)):
            conn.execute("INSERT OR IGNORE INTO entities (name) VALUES (?)", (name,))
            row = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_links (memory_id, entity_id) VALUES (?, ?)",
                    (memory_id, row[0]),
                )

    def delete(self, memory_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()

    # ── Busca RRF ─────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        use_bm25: bool = True,
        use_vector: bool = True,
        use_graph: bool = True,
    ) -> MemorySearchResult:
        modes_used: list[str] = []
        ranked: dict[int, dict[str, float]] = {}

        with self._conn() as conn:
            if use_bm25:
                for rank, (row_id, _) in enumerate(self._bm25(conn, query, top_k * 2)):
                    ranked.setdefault(row_id, {})["bm25"] = 1.0 / (_RRF_K + rank + 1)
                if ranked:
                    modes_used.append("bm25")

            if use_vector:
                for rank, (row_id, _) in enumerate(self._vector(conn, query, top_k * 2)):
                    ranked.setdefault(row_id, {})["vector"] = 1.0 / (_RRF_K + rank + 1)
                if "vector" not in modes_used and any("vector" in v for v in ranked.values()):
                    modes_used.append("vector")

            if use_graph:
                for rank, (row_id, _) in enumerate(self._graph(conn, query, top_k * 2)):
                    ranked.setdefault(row_id, {})["graph"] = 1.0 / (_RRF_K + rank + 1)
                if "graph" not in modes_used and any("graph" in v for v in ranked.values()):
                    modes_used.append("graph")

            fused = sorted(ranked.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:top_k]

            entries: list[MemoryEntry] = []
            for row_id, scores in fused:
                row = conn.execute(
                    "SELECT id, content, metadata FROM memories WHERE id = ?", (row_id,)
                ).fetchone()
                if row:
                    entries.append(MemoryEntry(
                        id=row["id"],
                        content=row["content"],
                        metadata=json.loads(row["metadata"]),
                        score=sum(scores.values()),
                    ))

        return MemorySearchResult(entries=entries, modes_used=modes_used)

    def _bm25(self, conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, float]]:
        try:
            rows = conn.execute(
                "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        except sqlite3.OperationalError:
            return []

    def _vector(self, conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, float]]:
        qvec = _embed(query)
        if qvec is None:
            return []
        rows = conn.execute("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL").fetchall()
        scored = []
        for row in rows:
            try:
                vec = json.loads(row["embedding"])
                scored.append((row["id"], _cosine(qvec, vec)))
            except (json.JSONDecodeError, TypeError):
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def _graph(self, conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, float]]:
        entities = set(_ENTITY_RE.findall(query))
        if not entities:
            return []
        placeholders = ",".join("?" * len(entities))
        rows = conn.execute(
            f"""
            SELECT el.memory_id, COUNT(*) AS shared
            FROM entity_links el
            JOIN entities e ON e.id = el.entity_id
            WHERE e.name IN ({placeholders})
            GROUP BY el.memory_id
            ORDER BY shared DESC
            LIMIT ?
            """,
            (*entities, limit),
        ).fetchall()
        return [(r["memory_id"], float(r["shared"])) for r in rows]

    # ── Integração com o pipeline de pesquisa ─────────────────────────────────

    def get_context(self, query: str, top_k: int = 5) -> str:
        """Retorna contexto formatado para injeção no prompt do LLM."""
        result = self.search(query, top_k=top_k)
        if not result.entries:
            return ""
        parts = [f"[Pesquisa anterior {i+1}] {e.content}" for i, e in enumerate(result.entries)]
        header = f"## Contexto de pesquisas anteriores (modos: {', '.join(result.modes_used)})\n\n"
        return header + "\n\n".join(parts)

    def store_research_result(
        self,
        query: str,
        executive_summary: str,
        top_entities: List[str],
        domain: str = "",
        duration_seconds: float = 0.0,
    ) -> int:
        """
        Persiste o resumo executivo + top-5 entidades de uma pesquisa concluída.
        Retorna o ID da memória criada.
        """
        entity_list = ", ".join(top_entities[:5]) if top_entities else "(nenhuma)"
        content = (
            f"Query: {query}\n"
            f"Domínio: {domain}\n"
            f"Entidades principais: {entity_list}\n\n"
            f"Resumo: {executive_summary[:800]}"
        )
        metadata = {
            "type": "research_result",
            "query": query,
            "domain": domain,
            "top_entities": top_entities[:5],
            "duration_seconds": round(duration_seconds, 1),
            "stored_at": datetime.now().isoformat(),
        }
        memory_id = self.add(content, metadata=metadata)
        logger.info(f"OrvixMemory: pesquisa '{query[:50]}' persistida (id={memory_id})")
        return memory_id

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0]
        return {"total_memories": total, "total_entities": entities, "total_links": links}
