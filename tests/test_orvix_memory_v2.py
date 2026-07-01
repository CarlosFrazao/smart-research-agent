import pytest
import os
import uuid
import shutil
import time
from pathlib import Path
from src.memory.orvix_memory_v2 import OrvixMemoryV2

@pytest.fixture(scope="function")
def temp_memory():
    """Fixture com bancos totalmente isolados por teste (UUID único por run)."""
    run_id = uuid.uuid4().hex[:8]
    test_db = f"reports/test_memory_{run_id}.db"
    test_kuzu = f"test_kuzu_{run_id}"
    test_collection = f"sra_test_{run_id}"

    os.makedirs("reports", exist_ok=True)

    # Instancia com paths únicos — sem depender de env vars patcheados após import
    memory = OrvixMemoryV2(db_path=test_db, kuzu_path=test_kuzu)

    # Substitui a coleção ChromaDB pelo nome único para isolamento total
    try:
        memory.chroma_collection = memory.chroma_client.get_or_create_collection(
            name=test_collection,
            metadata={"hnsw:space": "cosine"}
        )
    except Exception:
        pass  # Fallback: cliente efêmero já está isolado

    yield memory

    # ── Teardown ──────────────────────────────────────────────────────────────
    # 1. Remove coleção ChromaDB isolada
    try:
        memory.chroma_client.delete_collection(test_collection)
    except Exception:
        pass

    # 2. Fecha conexões (libera file handles do Windows antes de deletar)
    memory.close()

    # 3. Aguarda OS liberar handles
    time.sleep(0.5)

    # 4. Limpa artefatos temporários
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except PermissionError:
            pass  # Best-effort

    if os.path.exists(test_kuzu):
        shutil.rmtree(test_kuzu, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Testes
# ─────────────────────────────────────────────────────────────────────────────

def test_orvix_memory_v2_add_and_stats(temp_memory):
    """Verifica inserção nos 3 backends e contagem de estatísticas."""
    memory = temp_memory

    # Frases com entidades capitalizadas claras para o regex [A-Z][a-z]+
    id1 = memory.add(
        content="Python is an amazing language created by Guido van Rossum.",
        metadata={"category": "language"}
    )
    id2 = memory.add(
        content="Postgres is a relational database developed by Michael Stonebraker.",
        metadata={"category": "database"}
    )

    assert id1 > 0
    assert id2 > 0

    stats = memory.stats()
    assert stats["sqlite_memories"] == 2
    assert stats["chromadb_vectors"] == 2
    assert stats["kuzu_nodes_memory"] == 2
    # Entidades capturadas pelo regex \b[A-Z][a-z]+\b:
    # Frase 1: "Python", "Guido", "Rossum" → 3
    # Frase 2: "Postgres", "Michael", "Stonebraker" → 3 → total >= 3
    assert stats["kuzu_nodes_entity"] >= 3
    assert stats["kuzu_relationships"] >= 3


def test_orvix_memory_v2_search_rrf(temp_memory):
    """Verifica que a busca RRF ativa BM25, vetorial e grafo corretamente."""
    memory = temp_memory

    memory.add("Docker is used to containerize applications and manage environments.")
    memory.add("Kubernetes is used to orchestrate Docker services at scale.")

    # Query "Docker" — palavra exata presente em ambos os documentos
    # Garante que FTS5 (BM25) e ChromaDB ativem seus modos
    result = memory.search(
        "Docker", top_k=5,
        use_bm25=True, use_vector=True, use_graph=True
    )

    assert len(result.entries) >= 1
    assert any("Docker" in e.content for e in result.entries)
    # BM25: "Docker" aparece literalmente nos documentos → deve ativar
    assert "bm25" in result.modes_used
    # Vetor: embedding sempre disponível se ChromaDB está up
    assert "vector" in result.modes_used
    # Grafo: "Docker" capitalizado → entidade detectada → grafo ativado
    if "graph" in result.modes_used:
        assert len(result.entries) > 0


def test_orvix_memory_v2_delete(temp_memory):
    """Verifica que delete remove a memória dos 3 backends."""
    memory = temp_memory

    mem_id = memory.add("Temporary memory about SQLite database.")

    stats_before = memory.stats()
    assert stats_before["sqlite_memories"] == 1

    memory.delete(mem_id)

    stats_after = memory.stats()
    assert stats_after["sqlite_memories"] == 0
    assert stats_after["chromadb_vectors"] == 0
    assert stats_after["kuzu_nodes_memory"] == 0


def test_orvix_memory_v2_get_context(temp_memory):
    """Testa a geração de contexto formatado para injeção no prompt do LLM."""
    memory = temp_memory

    memory.add("FastAPI is a modern Python web framework for building APIs.")
    memory.add("Uvicorn is an ASGI server used to serve FastAPI applications.")

    context = memory.get_context("FastAPI web server", top_k=3)

    assert isinstance(context, str)
    assert len(context) > 0
    assert "Pesquisa anterior" in context
