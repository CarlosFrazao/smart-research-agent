import pytest
import numpy as np
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock
from src.types import SearchResult
from src.ranking.hybrid_ranker import HybridRanker, HybridRankerConfig

@pytest.mark.asyncio
async def test_hybrid_ranker_pipeline():
    # Setup mocks
    mock_transformer = MagicMock()
    # Mock do encode retornando arrays dummy
    mock_transformer.encode.return_value = np.array([
        [1.0, 0.0],  # query
        [1.0, 0.0],  # doc 1
        [0.0, 1.0],  # doc 2
    ])

    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = {
        "scores": [
            {"index": 0, "score": 95.0, "reason": "very good"},
            {"index": 1, "score": 40.0, "reason": "average"},
        ]
    }

    config = HybridRankerConfig(
        bm25_weight=0.30,
        embedding_weight=0.30,
        heuristic_weight=0.25,
        llm_weight=0.15,
        llm_top_k=5,
        enable_llm_rerank=True,
        pre_filter_threshold=0.0,  # Desabilita o pre-filtering para os itens do teste passarem
    )

    with patch("sentence_transformers.SentenceTransformer", return_value=mock_transformer):
        ranker = HybridRanker(llm_client=mock_llm, config=config)

    # Resultados brutos para ranquear
    now = datetime.now(timezone.utc)
    results = [
        SearchResult(
            source="github",
            title="FastAPI async framework",
            url="https://github.com/fastapi/fastapi",
            description="A very popular Python web framework.",
            metrics={},
            raw={},
            fetched_at=(now - timedelta(days=2)).isoformat(),
        ),
        SearchResult(
            source="reddit",
            title="Rust vs Go comparison",
            url="https://reddit.com/r/rust/comments/123",
            description="Discussing rust tools and general programming ideas.",
            metrics={},
            raw={},
            fetched_at=(now - timedelta(days=1)).isoformat(),
        )
    ]

    ranked = await ranker.rank(results, query="fastapi async python web")

    assert len(ranked) == 2

    # FastAPI deve ser o top 1 por causa do BM25 (overlap lexical) e Embeddings
    top_result = ranked[0]
    assert "FastAPI" in top_result.title
    assert top_result.score_breakdown["bm25"] > ranked[1].score_breakdown["bm25"]
    assert top_result.score_breakdown["embedding"] >= ranked[1].score_breakdown["embedding"]
    assert top_result.score_breakdown["authority"] == 0.95  # github NETLOC authority

    # Verifica chamada do LLM Re-ranker
    mock_llm.generate_structured.assert_called_once()
