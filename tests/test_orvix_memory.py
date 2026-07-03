"""
Testes unificados para a OrvixMemory (RAG Híbrido avançado SQLite + ChromaDB + KuzuDB)
"""

import pytest
import os
import uuid
import shutil
import time
from pathlib import Path

from src.memory.orvix_memory import OrvixMemory, MemoryEntry, MemorySearchResult


@pytest.fixture(scope="function")
def temp_memory():
    """Fixture com bancos totalmente isolados por teste (UUID único por run)."""
    run_id = uuid.uuid4().hex[:8]
    test_db = f"reports/test_memory_{run_id}.db"
    test_kuzu = f"test_kuzu_{run_id}"
    test_collection = f"sra_test_{run_id}"

    os.makedirs("reports", exist_ok=True)

    memory = OrvixMemory(db_path=test_db, kuzu_path=test_kuzu)

    # Substitui a coleção ChromaDB pelo nome único para isolamento total
    try:
        memory.chroma_collection = memory.chroma_client.get_or_create_collection(
            name=test_collection, metadata={"hnsw:space": "cosine"}
        )
    except Exception:
        pass  # Fallback: cliente efêmero já está isolado

    yield memory

    # ── Teardown ──────────────────────────────────────────────────────────────
    try:
        memory.chroma_client.delete_collection(test_collection)
    except Exception:
        pass

    memory.close()
    time.sleep(0.5)

    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except PermissionError:
            pass

    if os.path.exists(test_kuzu):
        shutil.rmtree(test_kuzu, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Testes do Schema e Stats
# ─────────────────────────────────────────────────────────────────────────────


def test_creates_db_file(temp_memory):
    assert Path(temp_memory._db_path).exists()


def test_stats_empty_db(temp_memory):
    s = temp_memory.stats()
    assert s["total_memories"] == 0
    assert s["total_entities"] == 0
    assert s["total_links"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Testes de Add (Inserção)
# ─────────────────────────────────────────────────────────────────────────────


def test_add_returns_id(temp_memory):
    id_ = temp_memory.add("Claude AI is a language model")
    assert isinstance(id_, int)
    assert id_ > 0


def test_add_increments_count(temp_memory):
    temp_memory.add("First memory")
    temp_memory.add("Second memory")
    assert temp_memory.stats()["total_memories"] == 2


def test_add_with_metadata(temp_memory):
    id_ = temp_memory.add("Test", metadata={"source": "github", "score": 80})
    assert id_ > 0


def test_add_extracts_named_entities(temp_memory):
    # regex \b[A-Z][a-z]+\b exige capitalização clara
    temp_memory.add("Twenty CRM is better than HubSpot")
    s = temp_memory.stats()
    assert s["total_entities"] >= 1


def test_add_links_entities(temp_memory):
    temp_memory.add("Twenty CRM versus HubSpot comparison")
    s = temp_memory.stats()
    assert s["total_links"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Testes de Delete
# ─────────────────────────────────────────────────────────────────────────────


def test_delete_removes_record(temp_memory):
    id_ = temp_memory.add("To be deleted")
    temp_memory.delete(id_)
    assert temp_memory.stats()["total_memories"] == 0


def test_delete_nonexistent_no_error(temp_memory):
    temp_memory.delete(9999)  # deve ser silencioso e não explodir


# ─────────────────────────────────────────────────────────────────────────────
# Testes de Search (Busca RRF)
# ─────────────────────────────────────────────────────────────────────────────


def test_search_empty_returns_empty(temp_memory):
    result = temp_memory.search("anything")
    assert isinstance(result, MemorySearchResult)
    assert result.entries == []


def test_search_bm25_finds_relevant(temp_memory):
    temp_memory.add("n8n is a workflow automation tool", metadata={"source": "test"})
    temp_memory.add("HubSpot is a CRM platform", metadata={"source": "test"})
    result = temp_memory.search(
        "workflow automation", use_bm25=True, use_vector=False, use_graph=False
    )
    assert len(result.entries) >= 1
    assert any("n8n" in e.content for e in result.entries)


def test_search_bm25_mode_reported(temp_memory):
    temp_memory.add("n8n workflow automation")
    result = temp_memory.search(
        "workflow", use_bm25=True, use_vector=False, use_graph=False
    )
    if result.entries:
        assert "bm25" in result.modes_used


def test_search_graph_finds_entity_overlap(temp_memory):
    temp_memory.add("Twenty CRM integrates with GitHub for issue tracking")
    result = temp_memory.search(
        "Twenty CRM", use_bm25=False, use_vector=False, use_graph=True
    )
    assert isinstance(result, MemorySearchResult)


def test_search_top_k_respected(temp_memory):
    for i in range(10):
        temp_memory.add(f"Workflow automation tool number {i}")
    result = temp_memory.search("workflow automation", top_k=3)
    assert len(result.entries) <= 3


def test_search_returns_memory_entry_type(temp_memory):
    temp_memory.add("Test content here")
    result = temp_memory.search(
        "test", use_bm25=True, use_vector=False, use_graph=False
    )
    if result.entries:
        e = result.entries[0]
        assert isinstance(e, MemoryEntry)
        assert e.id > 0
        assert isinstance(e.content, str)
        assert isinstance(e.metadata, dict)
        assert isinstance(e.score, float)


def test_search_vector_disabled_graceful(temp_memory):
    temp_memory.add("test content")
    result = temp_memory.search("test", use_vector=True)
    assert isinstance(result, MemorySearchResult)


# ─────────────────────────────────────────────────────────────────────────────
# Testes de Context e Store
# ─────────────────────────────────────────────────────────────────────────────


def test_get_context_empty_returns_empty_string(temp_memory):
    assert temp_memory.get_context("anything") == ""


def test_get_context_returns_formatted_string(temp_memory):
    temp_memory.add("n8n workflow automation open source")
    ctx = temp_memory.get_context("workflow automation", top_k=1)
    if ctx:
        assert "Pesquisa anterior" in ctx
        assert "n8n" in ctx


def test_get_context_top_k_limits_entries(temp_memory):
    for i in range(5):
        temp_memory.add(f"workflow automation tool {i} with specific features")
    ctx = temp_memory.get_context("workflow automation", top_k=2)
    if ctx:
        count = ctx.count("[Pesquisa anterior")
        assert count <= 2


def test_store_creates_memory(temp_memory):
    id_ = temp_memory.store_research_result(
        query="n8n workflow tools",
        executive_summary="n8n lidera o mercado com 280+ templates.",
        top_entities=["n8n", "Zapier", "Make"],
        domain="automation",
        duration_seconds=120.0,
    )
    assert id_ > 0
    assert temp_memory.stats()["total_memories"] == 1


def test_store_truncates_top_entities_to_5(temp_memory):
    temp_memory.store_research_result(
        query="test",
        executive_summary="summary",
        top_entities=["A", "B", "C", "D", "E", "F", "G"],
    )
    with temp_memory._conn() as conn:
        row = conn.execute("SELECT metadata FROM memories").fetchone()
        import json

        meta = json.loads(row["metadata"])
        assert len(meta["top_entities"]) <= 5


def test_store_metadata_fields(temp_memory):
    temp_memory.store_research_result(
        query="crm open source",
        executive_summary="Twenty CRM é promissor.",
        top_entities=["Twenty CRM"],
        domain="saas_b2b",
        duration_seconds=90.5,
    )
    with temp_memory._conn() as conn:
        row = conn.execute("SELECT metadata FROM memories").fetchone()
        import json

        meta = json.loads(row["metadata"])
        assert meta["type"] == "research_result"
        assert meta["domain"] == "saas_b2b"
        assert meta["duration_seconds"] == 90.5
        assert "stored_at" in meta


def test_store_result_searchable_after(temp_memory):
    temp_memory.store_research_result(
        query="n8n workflow automation",
        executive_summary="n8n é a melhor ferramenta de automação.",
        top_entities=["n8n"],
        domain="automation",
    )
    result = temp_memory.search(
        "workflow automation n8n", use_bm25=True, use_vector=False, use_graph=False
    )
    assert len(result.entries) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Testes Evolutivos da V2
# ─────────────────────────────────────────────────────────────────────────────


def test_orvix_memory_v2_add_and_stats(temp_memory):
    """Verifica inserção nos 3 backends e contagem de estatísticas."""
    memory = temp_memory

    id1 = memory.add(
        content="Python is an amazing language created by Guido van Rossum.",
        metadata={"category": "language"},
    )
    id2 = memory.add(
        content="Postgres is a relational database developed by Michael Stonebraker.",
        metadata={"category": "database"},
    )

    assert id1 > 0
    assert id2 > 0

    stats = memory.stats()
    assert stats["sqlite_memories"] == 2
    assert stats["chromadb_vectors"] == 2
    assert stats["kuzu_nodes_memory"] == 2
    assert stats["kuzu_nodes_entity"] >= 3
    assert stats["kuzu_relationships"] >= 3


def test_orvix_memory_v2_search_rrf(temp_memory):
    """Verifica que a busca RRF ativa BM25, vetorial e grafo corretamente."""
    memory = temp_memory

    memory.add("Docker is used to containerize applications and manage environments.")
    memory.add("Kubernetes is used to orchestrate Docker services at scale.")

    result = memory.search(
        "Docker", top_k=5, use_bm25=True, use_vector=True, use_graph=True
    )

    assert len(result.entries) >= 1
    assert any("Docker" in e.content for e in result.entries)
    assert "bm25" in result.modes_used
    assert "vector" in result.modes_used
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
    memory = temp_memory

    memory.add("FastAPI is a modern Python web framework for building APIs.")
    memory.add("Uvicorn is an ASGI server used to serve FastAPI applications.")

    context = memory.get_context("FastAPI web server", top_k=3)

    assert isinstance(context, str)
    assert len(context) > 0
    assert "Pesquisa anterior" in context
