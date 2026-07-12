"""Testes de integração do Cache refatorado: semantic hit, TTL adaptativo
inferido a partir da chave, e reaquecimento (warm_popular)."""

import tempfile

import pytest

from src.cache.cache import Cache
from src.utils.semantic_cache import SemanticCache


@pytest.fixture
def fake_embedder(monkeypatch):
    """Substitui o embedder real por um mapeamento determinístico de texto -> vetor,
    permitindo testar a integração semântica sem baixar nenhum modelo.

    Patcha o seam real do ``SemanticCache`` (``_get_embedding`` +
    ``_embedding_model_instance``) em vez de helpers de módulo inexistentes.
    """
    vectors = {
        "melhores frameworks Rust": [1.0, 0.0],
        "top Rust frameworks": [0.98, 0.02],  # quase idêntico -> deve bater
        "receita de bolo": [0.0, 1.0],  # nada a ver -> não deve bater
    }

    def fake_get_embedding(self, text: str):
        return vectors.get(text, [0.0, 0.0])

    def fake_init_model(self):
        # `enabled` depende de `_embedding_model_instance is not None`; um
        # sentinel basta porque `_get_embedding` está patchado e não o usa.
        self._embedding_model_instance = object()

    monkeypatch.setattr(SemanticCache, "_init_embedding_model", fake_init_model)
    monkeypatch.setattr(
        SemanticCache, "_get_embedding", fake_get_embedding, raising=True
    )


@pytest.mark.asyncio
async def test_semantic_hit_avoids_recompute(fake_embedder):
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_threshold=0.90)
        await cache.set("github", "melhores frameworks Rust", {"resultados": ["actix", "axum"]})

        # Query reformulada, semanticamente equivalente, nunca foi gravada com essa chave.
        result = await cache.get("github", "top Rust frameworks")
        assert result == {"resultados": ["actix", "axum"]}


@pytest.mark.asyncio
async def test_semantic_miss_for_unrelated_query(fake_embedder):
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_threshold=0.90)
        await cache.set("github", "melhores frameworks Rust", {"resultados": ["actix"]})

        result = await cache.get("github", "receita de bolo")
        assert result is None


@pytest.mark.asyncio
async def test_adaptive_ttl_inferred_from_search_service_key_pattern():
    """SearchService grava com prefix literal 'search' e a fonte real embutida
    na query (ex: 'github:consulta'). O bucket de TTL deve ser inferido corretamente."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_enabled=False)
        await cache.set("search", "github:consulta qualquer", [{"a": 1}])
        await cache.set("search", "reddit:consulta qualquer", [{"b": 2}])

        from datetime import datetime, timezone

        gh_key = "search:github:consulta qualquer"
        rd_key = "search:reddit:consulta qualquer"
        now = datetime.now(timezone.utc)

        gh_delta = (
            datetime.fromisoformat(cache.memory[gh_key]["expires"]) - now
        ).total_seconds()
        rd_delta = (
            datetime.fromisoformat(cache.memory[rd_key]["expires"]) - now
        ).total_seconds()

        assert gh_delta > 86300  # ~24h (github)
        assert 3500 < rd_delta < 3700  # ~1h (reddit)


@pytest.mark.asyncio
async def test_warm_popular_refreshes_frequently_accessed_keys():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_enabled=False, warm_threshold=2)
        await cache.set("q_popular", "valor_antigo", ttl_seconds=60)

        # Simula acessos repetidos (ultrapassa warm_threshold=2)
        await cache.get("q_popular")
        await cache.get("q_popular")
        await cache.get("q_popular")

        async def fetcher(key: str):
            assert key == "q_popular"
            return "valor_novo"

        warmed = await cache.warm_popular(fetcher, top_n=5)
        assert warmed == ["q_popular"]
        assert await cache.get("q_popular") == "valor_novo"


@pytest.mark.asyncio
async def test_warm_popular_ignores_keys_below_threshold():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_enabled=False, warm_threshold=5)
        await cache.set("q_raro", "valor", ttl_seconds=60)
        await cache.get("q_raro")  # apenas 1 acesso, abaixo do threshold=5

        async def fetcher(key: str):
            raise AssertionError("não deveria ser chamado para chaves não populares")

        warmed = await cache.warm_popular(fetcher, top_n=5)
        assert warmed == []


@pytest.mark.asyncio
async def test_popular_queries_ranking():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Cache(cache_dir=tmpdir, semantic_enabled=False)
        await cache.set("a", "1", ttl_seconds=60)
        await cache.set("b", "2", ttl_seconds=60)
        await cache.get("a")
        await cache.get("a")
        await cache.get("b")

        ranking = cache.popular_queries(top_n=2)
        keys_in_order = [k for k, _ in ranking]
        assert keys_in_order[0] == "a"  # "a" tem mais acessos que "b"
