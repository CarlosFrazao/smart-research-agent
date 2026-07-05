import os
import pytest
import shutil
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from src.types import SearchResult
from src.feedback_store import FeedbackStore
from src.ranker import QualityRanker
from src.ranking.learned_ranker import LearnedRanker, LearnedRankerConfig, LearnedRankerTrainer

@pytest.mark.asyncio
async def test_learned_ranker_pipeline(tmp_path):
    # Caminho do modelo temporário
    model_file = tmp_path / "ranker_test.pkl"
    feedback_file = tmp_path / "feedback_test.jsonl"

    # 1. Setup do FeedbackStore temporário
    store = FeedbackStore(store_path=str(feedback_file))

    # Escreve dados sintéticos para satisfazer o limite mínimo de 10 registros
    for i in range(12):
        store.record(
            result_id=f"res_{i}",
            query="python async programming",
            signal="useful" if i % 2 == 0 else "bookmark"
        )

    # 2. Configurações do Ranker
    config = LearnedRankerConfig(
        model_path=str(model_file),
        model_backend="sklearn",  # Ridge regression fallback leve e nativo do scikit-learn
        fallback_to_heuristic=True,
        embedding_model=None,  # Desabilita sentence-transformers para evitar download de pesos no teste
    )

    mock_heuristic = MagicMock(spec=QualityRanker)
    mock_heuristic.rank = AsyncMock()

    ranker = LearnedRanker(config=config, feedback_store=store, heuristic_ranker=mock_heuristic)

    # Testa fallback heurístico inicial (sem carregar o modelo)
    assert not ranker.is_ready()

    results = [
        SearchResult(
            source="github",
            title="Asyncio web framework",
            url="https://github.com/test/asyncio",
            description="High performance python async framework",
            metrics={"stars": 120, "forks": 15},
            raw={},
            fetched_at=datetime.now(UTC).isoformat()
        )
    ]

    await ranker.rank(results, query="python async")
    mock_heuristic.rank.assert_called_once_with(results)

    # 3. Treina o modelo ML usando o Trainer
    trainer = LearnedRankerTrainer(feedback_store=store, config=config)
    trained_path = await trainer.fit()

    assert trained_path.exists()
    assert trained_path.stat().st_size > 0

    # 4. Carrega o modelo no Ranker
    loaded = await ranker.load()
    assert loaded is True
    assert ranker.is_ready() is True

    # 5. Executa inferência ML real
    ranked = await ranker.rank(results, query="python async")

    assert len(ranked) == 1
    assert ranked[0].score_breakdown["learned_ranker"] is not None
    assert ranked[0].score_breakdown["bm25"] > 0
    assert ranked[0].score_breakdown["source_authority"] == 0.95  # github NETLOC authority

    # Feature importance lookup
    importances = ranker.get_feature_importance()
    assert importances is not None
    assert "bm25_score" in importances
