import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from src.utils.semantic_cache import SemanticCache, get_semantic_cache

@pytest.mark.asyncio
async def test_semantic_cache_in_memory_fallback():
    # Mock do SentenceTransformer
    mock_transformer = MagicMock()
    # Mock do encode para retornar um array de 384 dimensões
    mock_transformer.encode.return_value = np.zeros(384)

    with patch("sentence_transformers.SentenceTransformer", return_value=mock_transformer):
        cache = SemanticCache(similarity_threshold=0.90)

    assert cache.backend_name == "in_memory" or cache.backend_name == "chromadb"
    
    # Adiciona algo
    # Para cosseno funcionar, vamos mockar duas respostas diferentes:
    # Retorna [1.0, 0.0] para "query 1", e [0.95, 0.05] para "query 2" (similaridade alta)
    # E [0.0, 1.0] para "query 3" (similaridade zero)
    def mock_encode(text, **kwargs):
        vec = np.zeros(384)
        if "frameworks python" in text or "bibliotecas python" in text:
            vec[0] = 1.0
        elif "rust frameworks" in text:
            vec[1] = 1.0
        else:
            vec[0] = 0.5
            vec[1] = 0.5
        return vec

    mock_transformer.encode.side_effect = mock_encode

    # Grava no cache semântico usando get/set
    await cache.set("frameworks python", {"data": "python_frameworks_list"}, source="github")

    # Busca exata
    hit = await cache.get("frameworks python")
    assert hit is not None
    assert hit["data"] == {"data": "python_frameworks_list"}
    assert hit["similarity"] >= 0.90

    # Busca semântica similar
    hit_similar = await cache.get("bibliotecas python")
    assert hit_similar is not None
    assert hit_similar["data"] == {"data": "python_frameworks_list"}
    assert hit_similar["similarity"] >= 0.90

    # Busca não similar
    miss = await cache.get("rust frameworks")
    assert miss is None

    # Testando interface síncrona exigida pelo Cache legado (find/index)
    cache.index("prefix:frameworks python", {"data": "python_frameworks_list"}, prefix="prefix", ttl=3600)
    
    # Busca com find
    match = cache.find("bibliotecas python", prefix="prefix")
    assert match is not None
    matched_key, score = match
    assert matched_key == "prefix:frameworks python"
    assert score >= 0.90

    # Limpeza
    await cache.clear()
    assert cache.stats()["total_embeddings"] == 0
