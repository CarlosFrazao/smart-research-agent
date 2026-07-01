"""
OrvixMemoryV2 — Memória de RAG Híbrido Avançado (SQLite + ChromaDB + KuzuDB)

Evolução da OrvixMemory v1:
  - SQLite: Mantido para busca de texto completo rápida (BM25 via FTS5)
  - ChromaDB: Banco vetorial dedicado para busca semântica persistente de alta performance
  - KuzuDB: Banco de grafos embutido de altíssima velocidade para modelagem de entidades e relações
  - RRF: Fusão dos resultados de BM25 + Vetor (ChromaDB) + Grafo (KuzuDB via Cypher)

Mantém 100% de compatibilidade de interface com OrvixMemory v1.
"""
import os
import re
import json
import sqlite3
import logging
import threading
import chromadb
import kuzu
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Generator
from contextlib import contextmanager
from src.types import ResearchMetadata

logger = logging.getLogger(__name__)

# --- Configurações padrão ---
_RRF_K = 60
_EMBED_MODEL = "all-MiniLM-L6-v2"
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b")

_DEFAULT_DB_PATH = Path(os.environ.get("RESEARCH_MEMORY_DB", "reports/.research_memory.db"))
_CHROMA_HOST = os.environ.get("CHROMADB_HOST", "127.0.0.1")
_CHROMA_PORT = int(os.environ.get("CHROMADB_PORT", 3024))
_KUZU_PATH = os.environ.get("KUZU_DATA_PATH", "kuzu_data")

# --- Contratos de Dados (compatíveis com v1) ---
from src.memory.orvix_memory import MemoryEntry, MemorySearchResult, _get_embedder

class OrvixMemoryV2:
    """Memória de RAG Híbrido avançado combinando SQLite, ChromaDB e KuzuDB."""

    def __init__(self, db_path: Optional[str] = None, kuzu_path: Optional[str] = None) -> None:
        self._db_path = Path(db_path or str(_DEFAULT_DB_PATH))
        self._kuzu_path = kuzu_path or _KUZU_PATH
        self._local = threading.local()
        
        # 1. Inicializa o SQLite (para busca BM25 FTS5 e metadados)
        self._init_sqlite_schema()
        
        # 2. Inicializa o ChromaDB (vetorial)
        self._init_chroma()
        
        # 3. Inicializa o KuzuDB (grafos)
        self._init_kuzu()
        
        # 4. Inicializa o Grafo de Conhecimento Semântico (Bloco 3.2)
        from src.knowledge_graph import SemanticKnowledgeGraph
        self.kg = SemanticKnowledgeGraph(kuzu_conn=self.kuzu_conn)

    # ------------------------------------------------------------------
    # Inicializadores de Infraestrutura
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                self._db_path, check_same_thread=False, timeout=10
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield self._local.conn
        except Exception:
            self._local.conn.rollback()
            raise

    def _init_sqlite_schema(self) -> None:
        """Inicializa esquema mínimo do SQLite (apenas memórias e FTS5)."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    content    TEXT    NOT NULL,
                    metadata   TEXT    NOT NULL DEFAULT '{}',
                    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, content='memories', content_rowid='id');

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

    def _init_chroma(self) -> None:
        """Conecta ao ChromaDB e inicializa a coleção de memórias."""
        try:
            # Conecta ao container rodando em rede ou host
            self.chroma_client = chromadb.HttpClient(host=_CHROMA_HOST, port=_CHROMA_PORT)
            self.chroma_collection = self.chroma_client.get_or_create_collection(
                name="sra_memories",
                metadata={"hnsw:space": "cosine"}
            )
            logger.info(f"OrvixMemoryV2: Conectado ao ChromaDB em {_CHROMA_HOST}:{_CHROMA_PORT}")
        except Exception as e:
            logger.warning(f"OrvixMemoryV2: Falha ao conectar ao ChromaDB: {e}. Criando cliente efêmero local.")
            self.chroma_client = chromadb.Client()
            self.chroma_collection = self.chroma_client.get_or_create_collection("sra_memories")

    def _init_kuzu(self) -> None:
        """Inicializa o banco de grafos KuzuDB local."""
        try:
            # Cria a pasta de dados do Kuzu se não existir
            db_dir = Path(self._kuzu_path)
            db_dir.mkdir(parents=True, exist_ok=True)
            
            # O Kuzu v0.11.3 exige que o path seja o arquivo base de banco, não um diretório existente
            db_file_path = str(db_dir / "kuzu.db")
            
            self.kuzu_db = kuzu.Database(db_file_path)
            self.kuzu_conn = kuzu.Connection(self.kuzu_db)
            
            # Inicializa o schema do grafo (nós e arestas)
            # KuzuDB lançará erro se a tabela já existir, tratamos com try-except individual
            try:
                self.kuzu_conn.execute("CREATE NODE TABLE Memory(id INT64, content STRING, PRIMARY KEY(id))")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar tabela Memory: {e}")
                
            try:
                self.kuzu_conn.execute("CREATE NODE TABLE Entity(name STRING, PRIMARY KEY(name))")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar tabela Entity: {e}")
                
            try:
                self.kuzu_conn.execute("CREATE REL TABLE MENTIONED_IN(FROM Entity TO Memory)")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar relação MENTIONED_IN: {e}")

            # ── Evidence Graph tables ──────────────────────────────────────
            try:
                self.kuzu_conn.execute(
                    "CREATE NODE TABLE Claim("
                    "id STRING, text STRING, source STRING, "
                    "confidence DOUBLE, PRIMARY KEY(id))"
                )
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar tabela Claim: {e}")

            try:
                self.kuzu_conn.execute("CREATE REL TABLE CONFIRMS(FROM Claim TO Claim, weight DOUBLE)")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar relação CONFIRMS: {e}")

            try:
                self.kuzu_conn.execute("CREATE REL TABLE CONTRADICTS(FROM Claim TO Claim, divergence DOUBLE)")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar relação CONTRADICTS: {e}")

            # ── Semantic Knowledge Graph tables ────────────────────────────
            try:
                self.kuzu_conn.execute(
                    "CREATE NODE TABLE SemanticEntity(name STRING, type STRING, PRIMARY KEY(name))"
                )
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar tabela SemanticEntity: {e}")

            try:
                self.kuzu_conn.execute(
                    "CREATE REL TABLE RELATION(FROM SemanticEntity TO SemanticEntity, type STRING, confidence DOUBLE)"
                )
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"KuzuDB: Aviso ao criar relação RELATION: {e}")

            logger.info(f"OrvixMemoryV2: KuzuDB inicializado com sucesso em {db_file_path}")
        except Exception as e:
            logger.error(f"OrvixMemoryV2: Erro crítico ao inicializar KuzuDB: {e}")
            self.kuzu_db = None
            self.kuzu_conn = None

    # ------------------------------------------------------------------
    # Escrita de Memória
    # ------------------------------------------------------------------

    def add(self, content: str, metadata: Optional[dict] = None) -> int:
        """Persiste uma memória no SQLite, ChromaDB e KuzuDB."""
        meta = metadata or {}
        # Garante que o metadados não esteja vazio para evitar warnings/erros do ChromaDB
        if not meta:
            meta = {"source": "user_memory"}
        
        # 1. Salva no SQLite
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO memories (content, metadata) VALUES (?, ?)",
                (content, json.dumps(meta, default=str)),
            )
            memory_id: int = cur.lastrowid
            conn.commit()

        # 2. Salva no ChromaDB (gera embeddings via sentence-transformers local)
        try:
            from src.memory.orvix_memory import _embed
            embedding = _embed(content)
            if embedding:
                self.chroma_collection.add(
                    ids=[str(memory_id)],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[meta]
                )
        except Exception as e:
            logger.warning(f"OrvixMemoryV2: Erro ao indexar no ChromaDB: {e}")

        # 3. Salva no KuzuDB (atualiza grafo de entidades)
        if self.kuzu_conn:
            try:
                # Insere o nó da memória
                self.kuzu_conn.execute(
                    "CREATE (m:Memory {id: $id, content: $content})",
                    {"id": memory_id, "content": content}
                )
                
                # Extrai entidades do texto e cria relações
                entities = set(_ENTITY_RE.findall(content))
                for entity_name in entities:
                    # Insere a entidade (ignora duplicados)
                    self.kuzu_conn.execute(
                        "MERGE (e:Entity {name: $name})",
                        {"name": entity_name}
                    )
                    # Cria a aresta de relacionamento
                    self.kuzu_conn.execute(
                        "MATCH (e:Entity), (m:Memory) "
                        "WHERE e.name = $name AND m.id = $id "
                        "CREATE (e)-[:MENTIONED_IN]->(m)",
                        {"name": entity_name, "id": memory_id}
                    )

                # Extrai e adiciona triplas semânticas (Bloco 3.2)
                triples = self.kg.extract_triples(content)
                for triple in triples:
                    self.kg.add_triple(triple)
            except Exception as e:
                logger.warning(f"OrvixMemoryV2: Erro ao indexar no KuzuDB: {e}")

        logger.debug(f"OrvixMemoryV2: Gravado id={memory_id}")
        return memory_id

    def delete(self, memory_id: int) -> None:
        """Deleta a memória de todas as bases de dados."""
        # 1. SQLite
        with self._conn() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()

        # 2. ChromaDB
        try:
            self.chroma_collection.delete(ids=[str(memory_id)])
        except Exception as e:
            logger.warning(f"OrvixMemoryV2: Erro ao deletar do ChromaDB: {e}")

        # 3. KuzuDB
        if self.kuzu_conn:
            try:
                self.kuzu_conn.execute(
                    "MATCH (m:Memory) WHERE m.id = $id DETACH DELETE m",
                    {"id": memory_id}
                )
            except Exception as e:
                logger.warning(f"OrvixMemoryV2: Erro ao deletar do KuzuDB: {e}")

    # ------------------------------------------------------------------
    # Busca Híbrida RRF
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        use_bm25: bool = True,
        use_vector: bool = True,
        use_graph: bool = True,
    ) -> MemorySearchResult:
        modes_used: List[str] = []
        ranked: Dict[int, Dict[str, float]] = {}

        # Executa buscas em paralelo conceitual
        if use_bm25:
            with self._conn() as conn:
                bm25_results = self._bm25(conn, query, top_k * 2)
                for rank, (row_id, _) in enumerate(bm25_results):
                    ranked.setdefault(row_id, {})["bm25"] = 1.0 / (_RRF_K + rank + 1)
                if bm25_results:
                    modes_used.append("bm25")

        if use_vector:
            vector_results = self._vector(query, top_k * 2)
            for rank, (row_id, _) in enumerate(vector_results):
                ranked.setdefault(row_id, {})["vector"] = 1.0 / (_RRF_K + rank + 1)
            if vector_results:
                modes_used.append("vector")

        if use_graph and self.kuzu_conn:
            graph_results = self._graph(query, top_k * 2)
            for rank, (row_id, _) in enumerate(graph_results):
                ranked.setdefault(row_id, {})["graph"] = 1.0 / (_RRF_K + rank + 1)
            if graph_results:
                modes_used.append("graph")

        # Combina e ordena por RRF
        fused = sorted(ranked.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:top_k]

        entries: List[MemoryEntry] = []
        with self._conn() as conn:
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

    # --- Motores de Busca Individuais ---

    def _bm25(self, conn: sqlite3.Connection, query: str, limit: int) -> List[Tuple[int, float]]:
        try:
            rows = conn.execute(
                "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        except sqlite3.OperationalError:
            return []

    def _vector(self, query: str, limit: int) -> List[Tuple[int, float]]:
        """Busca vetorial de alta performance usando ChromaDB."""
        try:
            from src.memory.orvix_memory import _embed
            qvec = _embed(query)
            if qvec is None:
                return []
                
            results = self.chroma_collection.query(
                query_embeddings=[qvec],
                n_results=limit
            )
            
            scored = []
            if results and results.get("ids") and results["ids"][0]:
                for doc_id, dist in zip(results["ids"][0], results["distances"][0]):
                    # Distância do cosseno convertida para score de similaridade
                    similarity = 1.0 - dist
                    scored.append((int(doc_id), similarity))
            return scored
        except Exception as e:
            logger.warning(f"OrvixMemoryV2: Busca vetorial no ChromaDB falhou: {e}")
            return []

    def _graph(self, query: str, limit: int) -> List[Tuple[int, float]]:
        """Busca semântica no banco de grafos KuzuDB usando Cypher."""
        if not self.kuzu_conn:
            return []
            
        entities = set(_ENTITY_RE.findall(query))
        if not entities:
            return []
            
        try:
            # Cypher query: Encontra memórias que compartilham entidades mencionadas na query
            entity_list = list(entities)
            result = self.kuzu_conn.execute(
                "MATCH (e:Entity)-[:MENTIONED_IN]->(m:Memory) "
                "WHERE e.name IN $entities "
                "RETURN m.id, count(e) AS shared "
                "ORDER BY shared DESC "
                "LIMIT $limit",
                {"entities": entity_list, "limit": limit}
            )
            
            scored = []
            while result.has_next():
                row = result.get_next()
                scored.append((int(row[0]), float(row[1])))
            return scored
        except Exception as e:
            logger.warning(f"OrvixMemoryV2: Busca de grafos no KuzuDB falhou: {e}")
            return []

    # ------------------------------------------------------------------
    # Métodos de Compatibilidade de Interface
    # ------------------------------------------------------------------

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
        """Persiste o resumo executivo + top-5 entidades de uma pesquisa concluída."""
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
        logger.info(f"OrvixMemoryV2: pesquisa '{query[:50]}' persistida (id={memory_id})")
        return memory_id

    def close(self) -> None:
        """Fecha todas as conexões abertas (SQLite thread-local, KuzuDB)."""
        # Fecha SQLite do thread local
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
        # Fecha KuzuDB
        if self.kuzu_conn is not None:
            self.kuzu_conn = None
        if self.kuzu_db is not None:
            self.kuzu_db = None

    def stats(self) -> dict:
        """Retorna estatísticas acumuladas dos três bancos de dados."""
        with self._conn() as conn:
            sqlite_total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            
        chroma_total = 0
        try:
            chroma_total = self.chroma_collection.count()
        except Exception:
            pass
            
        kuzu_memories = 0
        kuzu_entities = 0
        kuzu_rels = 0
        if self.kuzu_conn:
            try:
                res = self.kuzu_conn.execute("MATCH (m:Memory) RETURN count(*)")
                if res.has_next(): kuzu_memories = res.get_next()[0]
                
                res = self.kuzu_conn.execute("MATCH (e:Entity) RETURN count(*)")
                if res.has_next(): kuzu_entities = res.get_next()[0]
                
                res = self.kuzu_conn.execute("MATCH ()-[r:MENTIONED_IN]->() RETURN count(*)")
                if res.has_next(): kuzu_rels = res.get_next()[0]
            except Exception:
                pass
                
        return {
            "sqlite_memories": sqlite_total,
            "chromadb_vectors": chroma_total,
            "kuzu_nodes_memory": kuzu_memories,
            "kuzu_nodes_entity": kuzu_entities,
            "kuzu_relationships": kuzu_rels
        }
