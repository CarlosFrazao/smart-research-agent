import pytest
import os
import uuid
import shutil
import time
from unittest.mock import AsyncMock, MagicMock
from src.memory.orvix_memory_v2 import OrvixMemoryV2
from src.knowledge_graph import SemanticKnowledgeGraph, Triple
from src.clients.llm_client import LLMClient


@pytest.fixture(scope="function")
def temp_memory():
    """Fixture com banco de grafos KuzuDB isolado por teste."""
    run_id = uuid.uuid4().hex[:8]
    test_db = f"reports/test_kg_memory_{run_id}.db"
    test_kuzu = f"test_kg_kuzu_{run_id}"
    test_collection = f"sra_kg_test_{run_id}"

    os.makedirs("reports", exist_ok=True)
    memory = OrvixMemoryV2(db_path=test_db, kuzu_path=test_kuzu)

    try:
        memory.chroma_collection = memory.chroma_client.get_or_create_collection(
            name=test_collection,
            metadata={"hnsw:space": "cosine"}
        )
    except Exception:
        pass

    yield memory

    memory.close()
    time.sleep(0.5)

    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except PermissionError:
            pass

    if os.path.exists(test_kuzu):
        shutil.rmtree(test_kuzu, ignore_errors=True)


def test_kg_regex_extraction():
    kg = SemanticKnowledgeGraph()
    
    # Testa detecção founded_by
    t1 = kg.extract_triples("Google was founded by Larry Page and Sergey Brin.")
    assert len(t1) >= 1
    assert any(t.relation == "founded_by" and t.subject == "Google" and "Larry Page" in t.object for t in t1)

    # Testa detecção produces
    t2 = kg.extract_triples("Apple produces iPhone.")
    assert len(t2) == 1
    assert t2[0].subject == "Apple"
    assert t2[0].relation == "produces"
    assert t2[0].object == "iPhone"

    # Testa detecção competes_with
    t3 = kg.extract_triples("Prisma competes with SQLAlchemy.")
    assert len(t3) == 1
    assert t3[0].subject == "Prisma"
    assert t3[0].relation == "competes_with"
    assert t3[0].object == "SQLAlchemy"


@pytest.mark.asyncio
async def test_kg_llm_extraction():
    llm_mock = MagicMock(spec=LLMClient)
    llm_mock.generate = AsyncMock(return_value="""
[
  {
    "subject": "React",
    "relation": "created_by",
    "object": "Meta",
    "confidence": 0.95
  },
  {
    "subject": "Svelte",
    "relation": "competes_with",
    "object": "React",
    "confidence": 0.85
  }
]
""")
    kg = SemanticKnowledgeGraph(llm_client=llm_mock)
    triples = await kg.extract_triples_with_llm("React is created by Meta. Svelte competes with React.")
    
    assert len(triples) == 2
    assert triples[0].subject == "React"
    assert triples[0].relation == "created_by"
    assert triples[0].object == "Meta"
    assert triples[0].confidence == 0.95
    assert triples[1].relation == "competes_with"


def test_kg_kuzu_integration(temp_memory):
    memory = temp_memory
    kg = memory.kg  # SemanticKnowledgeGraph instanciado automaticamente

    # Testa adição direta de tripla
    triple = Triple(
        subject="Python",
        relation="created_by",
        object="Guido",
        confidence=0.9,
        source="test"
    )
    kg.add_triple(triple)

    # Consulta no grafo
    triples = kg.query_graph(subject="Python")
    assert len(triples) == 1
    assert triples[0].relation == "created_by"
    assert triples[0].object == "Guido"
    assert triples[0].confidence == 0.9

    # Consulta por relação
    triples_rel = kg.query_graph(relation="created_by")
    assert len(triples_rel) == 1
    assert triples_rel[0].subject == "Python"

    # Testa entidades relacionadas
    related = kg.get_related_entities("Python")
    assert "Guido" in related


def test_kg_add_via_memory(temp_memory):
    memory = temp_memory
    kg = memory.kg

    # Ao adicionar memória, triplas devem ser extraídas por regex heurística e salvas no Kuzu
    # "Apple produces iPhone" aciona produces
    memory.add("Apple produces iPhone for customers.")

    triples = kg.query_graph(relation="produces")
    assert len(triples) == 1
    assert triples[0].subject == "Apple"
    assert triples[0].object == "iPhone"


def test_kg_export_ttl_and_json(temp_memory):
    kg = temp_memory.kg

    kg.add_triple(Triple(
        subject="Tesla",
        relation="competes_with",
        object="BYD",
        confidence=0.8,
        source="test"
    ))

    # Export RDF/Turtle
    ttl = kg.export_ttl()
    assert "@prefix sra:" in ttl
    assert "sra:Tesla srap:competes_with sra:BYD ." in ttl

    # Export JSON
    js = kg.export_json()
    assert len(js["nodes"]) == 2
    assert len(js["links"]) == 1
    assert js["links"][0]["source"] == "Tesla"
    assert js["links"][0]["target"] == "BYD"
    assert js["links"][0]["type"] == "competes_with"
