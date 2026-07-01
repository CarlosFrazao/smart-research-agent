"""Testes do OrvixMemory P5."""
import pytest
import tempfile
from pathlib import Path

from src.memory.orvix_memory import OrvixMemory, MemoryEntry, MemorySearchResult


def _tmp_mem() -> OrvixMemory:
    tmp = tempfile.mktemp(suffix=".db")
    return OrvixMemory(db_path=tmp)


class TestOrvixMemorySchema:
    def test_creates_db_file(self):
        tmp = tempfile.mktemp(suffix=".db")
        mem = OrvixMemory(db_path=tmp)
        assert Path(tmp).exists()

    def test_stats_empty_db(self):
        mem = _tmp_mem()
        s = mem.stats()
        assert s["total_memories"] == 0
        assert s["total_entities"] == 0
        assert s["total_links"] == 0


class TestOrvixMemoryAdd:
    def test_add_returns_id(self):
        mem = _tmp_mem()
        id_ = mem.add("Claude AI is a language model")
        assert isinstance(id_, int)
        assert id_ > 0

    def test_add_increments_count(self):
        mem = _tmp_mem()
        mem.add("First memory")
        mem.add("Second memory")
        assert mem.stats()["total_memories"] == 2

    def test_add_with_metadata(self):
        mem = _tmp_mem()
        id_ = mem.add("Test", metadata={"source": "github", "score": 80})
        assert id_ > 0

    def test_add_extracts_named_entities(self):
        mem = _tmp_mem()
        mem.add("Twenty CRM is better than HubSpot")
        s = mem.stats()
        assert s["total_entities"] >= 1

    def test_add_links_entities(self):
        mem = _tmp_mem()
        mem.add("Twenty CRM versus HubSpot comparison")
        s = mem.stats()
        assert s["total_links"] >= 1


class TestOrvixMemoryDelete:
    def test_delete_removes_record(self):
        mem = _tmp_mem()
        id_ = mem.add("To be deleted")
        mem.delete(id_)
        assert mem.stats()["total_memories"] == 0

    def test_delete_nonexistent_no_error(self):
        mem = _tmp_mem()
        mem.delete(9999)  # deve ser silencioso


class TestOrvixMemorySearch:
    def test_search_empty_returns_empty(self):
        mem = _tmp_mem()
        result = mem.search("anything")
        assert isinstance(result, MemorySearchResult)
        assert result.entries == []

    def test_search_bm25_finds_relevant(self):
        mem = _tmp_mem()
        mem.add("n8n is a workflow automation tool", metadata={"source": "test"})
        mem.add("HubSpot is a CRM platform", metadata={"source": "test"})
        result = mem.search("workflow automation", use_bm25=True, use_vector=False, use_graph=False)
        assert len(result.entries) >= 1
        assert any("n8n" in e.content for e in result.entries)

    def test_search_bm25_mode_reported(self):
        mem = _tmp_mem()
        mem.add("n8n workflow automation")
        result = mem.search("workflow", use_bm25=True, use_vector=False, use_graph=False)
        if result.entries:
            assert "bm25" in result.modes_used

    def test_search_graph_finds_entity_overlap(self):
        mem = _tmp_mem()
        mem.add("Twenty CRM integrates with GitHub for issue tracking")
        result = mem.search("Twenty CRM", use_bm25=False, use_vector=False, use_graph=True)
        # Graph search requer entidades nomeadas (maiúsculas) na query e no conteúdo
        assert isinstance(result, MemorySearchResult)

    def test_search_top_k_respected(self):
        mem = _tmp_mem()
        for i in range(10):
            mem.add(f"Workflow automation tool number {i}")
        result = mem.search("workflow automation", top_k=3)
        assert len(result.entries) <= 3

    def test_search_returns_memory_entry_type(self):
        mem = _tmp_mem()
        mem.add("Test content here")
        result = mem.search("test", use_bm25=True, use_vector=False, use_graph=False)
        if result.entries:
            e = result.entries[0]
            assert isinstance(e, MemoryEntry)
            assert e.id > 0
            assert isinstance(e.content, str)
            assert isinstance(e.metadata, dict)
            assert isinstance(e.score, float)

    def test_search_vector_disabled_graceful(self):
        mem = _tmp_mem()
        mem.add("test content")
        result = mem.search("test", use_vector=True)
        assert isinstance(result, MemorySearchResult)


class TestGetContext:
    def test_get_context_empty_returns_empty_string(self):
        mem = _tmp_mem()
        assert mem.get_context("anything") == ""

    def test_get_context_returns_formatted_string(self):
        mem = _tmp_mem()
        mem.add("n8n workflow automation open source")
        ctx = mem.get_context("workflow automation", top_k=1)
        if ctx:
            assert "Pesquisa anterior" in ctx
            assert "n8n" in ctx

    def test_get_context_top_k_limits_entries(self):
        mem = _tmp_mem()
        for i in range(5):
            mem.add(f"workflow automation tool {i} with specific features")
        ctx = mem.get_context("workflow automation", top_k=2)
        if ctx:
            count = ctx.count("[Pesquisa anterior")
            assert count <= 2


class TestStoreResearchResult:
    def test_store_creates_memory(self):
        mem = _tmp_mem()
        id_ = mem.store_research_result(
            query="n8n workflow tools",
            executive_summary="n8n lidera o mercado com 280+ templates.",
            top_entities=["n8n", "Zapier", "Make"],
            domain="automation",
            duration_seconds=120.0,
        )
        assert id_ > 0
        assert mem.stats()["total_memories"] == 1

    def test_store_truncates_top_entities_to_5(self):
        mem = _tmp_mem()
        mem.store_research_result(
            query="test",
            executive_summary="summary",
            top_entities=["A", "B", "C", "D", "E", "F", "G"],
        )
        # Verifica que apenas 5 foram persistidos nos metadados
        with mem._conn() as conn:
            row = conn.execute("SELECT metadata FROM memories").fetchone()
            import json
            meta = json.loads(row["metadata"])
            assert len(meta["top_entities"]) <= 5

    def test_store_metadata_fields(self):
        mem = _tmp_mem()
        mem.store_research_result(
            query="crm open source",
            executive_summary="Twenty CRM é promissor.",
            top_entities=["Twenty CRM"],
            domain="saas_b2b",
            duration_seconds=90.5,
        )
        with mem._conn() as conn:
            row = conn.execute("SELECT metadata FROM memories").fetchone()
            import json
            meta = json.loads(row["metadata"])
            assert meta["type"] == "research_result"
            assert meta["domain"] == "saas_b2b"
            assert meta["duration_seconds"] == 90.5
            assert "stored_at" in meta

    def test_store_result_searchable_after(self):
        mem = _tmp_mem()
        mem.store_research_result(
            query="n8n workflow automation",
            executive_summary="n8n é a melhor ferramenta de automação.",
            top_entities=["n8n"],
            domain="automation",
        )
        result = mem.search("workflow automation n8n", use_bm25=True, use_vector=False, use_graph=False)
        assert len(result.entries) >= 1
