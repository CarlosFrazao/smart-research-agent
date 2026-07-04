"""Pacote de ranking hibrido (heuristicas + BM25 + embeddings), sem LLM.

Ver `src/ranking/hybrid_ranker.py` para a implementacao e o racional.
"""

from src.ranking.hybrid_ranker import (
    BM25,
    DEFAULT_PRE_FILTER_TOP_N,
    DEFAULT_WEIGHTS,
    HybridRankResult,
    HybridRanker,
    SemanticScorer,
)

__all__ = [
    "BM25",
    "DEFAULT_PRE_FILTER_TOP_N",
    "DEFAULT_WEIGHTS",
    "HybridRankResult",
    "HybridRanker",
    "SemanticScorer",
]
