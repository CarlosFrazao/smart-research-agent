"""Testes para src/utils/semantic_cache.py."""

import pytest

from src.utils import semantic_cache as sc


def test_cosine_similarity_identical_vectors():
    v = [1.0, 0.0, 0.0]
    assert sc.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert sc.cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_is_safe():
    a = [0.0, 0.0]
    b = [1.0, 1.0]
    assert sc.cosine_similarity(a, b) == 0.0


def test_semantic_cache_disabled_without_embedder(monkeypatch):
    """Sem sentence-transformers instalado, o índice deve ficar desabilitado
    e toda consulta deve retornar None sem lançar exceção."""
    monkeypatch.setattr(sc, "_get_embedder", lambda: None)
    cache = sc.SemanticCache()
    assert cache.enabled is False
    cache.index("query qualquer", key="k1")
    assert cache.find("query qualquer") is None


def test_semantic_cache_find_above_threshold(monkeypatch):
    """Injeta embeddings fake determinísticos para validar a lógica de
    indexação/busca sem depender do modelo real."""
    fake_vectors = {
        "melhores frameworks Rust 2025": [1.0, 0.0, 0.0],
        "top Rust frameworks this year": [0.95, 0.05, 0.0],  # bem similar
        "receita de bolo de cenoura": [0.0, 0.0, 1.0],  # nada a ver
    }

    def fake_embed(text: str):
        return fake_vectors.get(text)

    monkeypatch.setattr(sc, "_get_embedder", lambda: object())
    monkeypatch.setattr(sc, "embed", fake_embed)

    cache = sc.SemanticCache(threshold=0.90)
    cache.index("melhores frameworks Rust 2025", key="github:melhores frameworks Rust 2025", prefix="github")

    match = cache.find("top Rust frameworks this year", prefix="github")
    assert match is not None
    matched_key, score = match
    assert matched_key == "github:melhores frameworks Rust 2025"
    assert score >= 0.90

    miss = cache.find("receita de bolo de cenoura", prefix="github")
    assert miss is None


def test_semantic_cache_respects_prefix_isolation(monkeypatch):
    """Duas queries idênticas em prefixes diferentes não devem se misturar —
    evita falso positivo entre fontes distintas (ex: github vs reddit)."""
    vec = [1.0, 0.0]
    monkeypatch.setattr(sc, "_get_embedder", lambda: object())
    monkeypatch.setattr(sc, "embed", lambda text: vec)

    cache = sc.SemanticCache(threshold=0.90)
    cache.index("consulta identica", key="github:consulta identica", prefix="github")

    assert cache.find("consulta identica", prefix="reddit") is None
    match = cache.find("consulta identica", prefix="github")
    assert match is not None
    assert match[0] == "github:consulta identica"


def test_semantic_cache_index_overwrites_same_key(monkeypatch):
    monkeypatch.setattr(sc, "_get_embedder", lambda: object())
    monkeypatch.setattr(sc, "embed", lambda text: [1.0, 0.0])

    cache = sc.SemanticCache(threshold=0.90)
    cache.index("query v1", key="k1", prefix="github")
    cache.index("query v2", key="k1", prefix="github")

    assert len(cache._entries) == 1
    assert cache._entries[0].query == "query v2"


def test_semantic_cache_remove_and_clear(monkeypatch):
    vectors = {"q1": [1.0, 0.0], "q2": [0.0, 1.0]}
    monkeypatch.setattr(sc, "_get_embedder", lambda: object())
    monkeypatch.setattr(sc, "embed", lambda text: vectors[text])

    cache = sc.SemanticCache(threshold=0.90)
    cache.index("q1", key="k1", prefix="github")
    cache.index("q2", key="k2", prefix="github")

    cache.remove("k1")
    assert cache.find("q1", prefix="github") is None
    assert cache.find("q2", prefix="github") is not None

    cache.clear()
    assert cache.find("q2", prefix="github") is None
