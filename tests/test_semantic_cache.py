"""Testes para src/utils/semantic_cache.py.

Exercitam a API real do ``SemanticCache`` (síncrona: ``index``/``find``/
``remove``), forçando o backend em memória e injetando um encoder fake
determinístico via ``_embedding_model_instance`` — o mesmo seam que o código
de produção usa (``_get_embedding``), sem depender de sentence-transformers
nem de ChromaDB.
"""

import numpy as np
import pytest

from src.utils import semantic_cache as sc
from src.utils.semantic_cache import SemanticCache, _InMemoryEmbeddingStore


class _FakeEncoder:
    """Encoder determinístico: mapeia texto -> vetor via tabela fornecida."""

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def encode(self, text, **kwargs):
        vec = self._vectors.get(text)
        if vec is None:
            raise KeyError(f"vetor não definido para: {text!r}")
        return np.array(vec, dtype=float)


def _make_cache(vectors: dict[str, list[float]], threshold: float = 0.90) -> SemanticCache:
    """Cria um SemanticCache com backend em memória e encoder fake.

    Evita a inicialização real de sentence-transformers/ChromaDB substituindo
    o modelo de embeddings e o backend após a construção.
    """
    cache = SemanticCache(threshold=threshold)
    cache._embedding_model_instance = _FakeEncoder(vectors)
    cache._backend = _InMemoryEmbeddingStore()
    cache._is_chromadb = False
    return cache


# ── Similaridade cosseno (via _InMemoryEmbeddingStore.search) ────────────────


def test_cosine_identical_vectors_scores_one():
    store = _InMemoryEmbeddingStore()
    store.add(query="q", embedding=[1.0, 0.0, 0.0], data={}, metadata={})
    hits = store.search([1.0, 0.0, 0.0], threshold=0.99, limit=1)
    assert hits and hits[0]["similarity"] == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_scores_zero():
    store = _InMemoryEmbeddingStore()
    store.add(query="q", embedding=[1.0, 0.0], data={}, metadata={})
    # ortogonal -> similaridade 0, abaixo de qualquer threshold > 0
    hits = store.search([0.0, 1.0], threshold=0.01, limit=1)
    assert hits == []


def test_cosine_zero_vector_is_safe():
    store = _InMemoryEmbeddingStore()
    store.add(query="q", embedding=[0.0, 0.0], data={}, metadata={})
    # não deve levantar ZeroDivisionError; similaridade tratada como 0.0
    hits = store.search([1.0, 1.0], threshold=0.0, limit=1)
    assert hits and hits[0]["similarity"] == 0.0


# ── enabled / desabilitado sem embedder ──────────────────────────────────────


def test_disabled_without_embedder():
    """Sem modelo de embeddings, o cache fica desabilitado e find() retorna None."""
    cache = SemanticCache(threshold=0.90)
    cache._embedding_model_instance = None
    cache._backend = _InMemoryEmbeddingStore()
    cache._is_chromadb = False

    assert cache.enabled is False
    cache.index("query qualquer", "github:query qualquer", prefix="github")
    assert cache.find("query qualquer", prefix="github") is None


# ── find acima do threshold ──────────────────────────────────────────────────


def test_find_above_threshold():
    vectors = {
        "melhores frameworks Rust 2025": [1.0, 0.0, 0.0],
        "top Rust frameworks this year": [0.95, 0.05, 0.0],  # bem similar
        "receita de bolo de cenoura": [0.0, 0.0, 1.0],  # nada a ver
    }
    cache = _make_cache(vectors, threshold=0.90)
    # index(query_text, cache_key, prefix): a query é vetorizada; a key é o id
    # estável retornado por find/removido por remove.
    cache.index(
        "melhores frameworks Rust 2025",
        "github:melhores frameworks Rust 2025",
        prefix="github",
    )

    match = cache.find("top Rust frameworks this year", prefix="github")
    assert match is not None
    matched_key, score = match
    assert matched_key == "github:melhores frameworks Rust 2025"
    assert score >= 0.90

    miss = cache.find("receita de bolo de cenoura", prefix="github")
    assert miss is None


# ── isolamento por prefix ────────────────────────────────────────────────────


def test_respects_prefix_isolation():
    vectors = {"consulta identica": [1.0, 0.0]}
    cache = _make_cache(vectors, threshold=0.90)
    cache.index("consulta identica", "github:consulta identica", prefix="github")

    assert cache.find("consulta identica", prefix="reddit") is None
    match = cache.find("consulta identica", prefix="github")
    assert match is not None
    assert match[0] == "github:consulta identica"


# ── overwrite da mesma key ───────────────────────────────────────────────────


def test_index_overwrites_same_key():
    # mesma cache_key ("github:k1") indexada duas vezes -> uma única entrada
    # (o id é o hash da key estável).
    vectors = {"query um": [1.0, 0.0], "query dois": [1.0, 0.0]}
    cache = _make_cache(vectors, threshold=0.90)
    cache.index("query um", "github:k1", prefix="github")
    cache.index("query dois", "github:k1", prefix="github")

    assert cache._backend.count() == 1


# ── remove e clear ───────────────────────────────────────────────────────────


def test_remove_and_clear():
    vectors = {"q1": [1.0, 0.0], "q2": [0.0, 1.0]}
    cache = _make_cache(vectors, threshold=0.90)
    cache.index("q1", "github:q1", prefix="github")
    cache.index("q2", "github:q2", prefix="github")

    # remove pela cache_key completa (como faz a camada Cache)
    cache.remove("github:q1")
    assert cache.find("q1", prefix="github") is None
    assert cache.find("q2", prefix="github") is not None

    cache._backend.clear()
    assert cache.find("q2", prefix="github") is None
