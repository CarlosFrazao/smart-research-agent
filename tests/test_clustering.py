"""Testes do algoritmo de clustering de resultados (Fase 4).

Valida a função `cluster_similar_results` em `src/pipeline/stages/rank_stage.py`,
que agrupa resultados de fontes diferentes sobre o mesmo fato/evento usando
embeddings de cosseno já calculados pelo HybridRanker.
"""

from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import MagicMock

from src.pipeline.stages.rank_stage import cluster_similar_results
from src.types import RankedResult


class TestClusterSimilarResults:
    """Testes unitários do algoritmo de clustering."""

    def _make_result(self, source: str, url: str, score: float = 50.0) -> RankedResult:
        """Cria um RankedResult sintético com os campos usados no clustering."""
        return RankedResult(
            source=source,
            url=url,
            title=f"Resultado de {source}",
            description=f"Conteúdo sobre o mesmo evento em {source}",
            score=score,
        )

    def _embeddings(
        self, urls: list[str], similar_pairs: list[tuple[str, str]] | None = None
    ) -> dict[str, np.ndarray]:
        """Cria embeddings onde pares listados têm similaridade > 0.88.

        Args:
            urls: Lista de URLs para gerar embeddings unitários aleatórios.
            similar_pairs: Pares (url_a, url_b) que devem ter alta similaridade.

        Returns:
            dict[str, np.ndarray]: Mapa url -> vetor normalizado.
        """
        embs: dict[str, np.ndarray] = {}
        for url in urls:
            vec = np.random.randn(384)
            embs[url] = vec / np.linalg.norm(vec)
        # Força similaridade alta entre pares especificados
        if similar_pairs:
            for u1, u2 in similar_pairs:
                embs[u2] = embs[u1] + np.random.randn(384) * 0.01
                embs[u2] = embs[u2] / np.linalg.norm(embs[u2])
        return embs

    def test_imports_ok(self):
        """Smoke test: função deve ser importável."""
        assert callable(cluster_similar_results)

    def test_same_source_not_clustered(self):
        """Resultados da MESMA fonte NUNCA devem ser agrupados."""
        r1 = self._make_result("reddit", "https://reddit.com/1")
        r2 = self._make_result("reddit", "https://reddit.com/2")
        embs = self._embeddings(
            ["https://reddit.com/1", "https://reddit.com/2"],
            similar_pairs=[("https://reddit.com/1", "https://reddit.com/2")],
        )
        cluster_similar_results([r1, r2], embs)
        assert r1.cluster_id is None
        assert r2.cluster_id is None

    def test_different_sources_similar_content_clustered(self):
        """Resultados de fontes DIFERENTES com conteúdo similar devem agrupar."""
        r1 = self._make_result("reddit", "https://reddit.com/news")
        r2 = self._make_result("hackernews", "https://hn.com/news")
        embs = self._embeddings(
            ["https://reddit.com/news", "https://hn.com/news"],
            similar_pairs=[("https://reddit.com/news", "https://hn.com/news")],
        )
        cluster_similar_results([r1, r2], embs)
        assert r1.cluster_id == r2.cluster_id
        assert r1.cluster_id is not None
        assert "hackernews" in r1.corroborated_by
        assert "reddit" in r2.corroborated_by

    def test_dissimilar_content_not_clustered(self):
        """Resultados de fontes diferentes com conteúdo diferente NÃO agrupam."""
        r1 = self._make_result("reddit", "https://reddit.com/a")
        r2 = self._make_result("hackernews", "https://hn.com/b")
        # Sem similar_pairs → embeddings aleatórios e dissimilares
        embs = self._embeddings(["https://reddit.com/a", "https://hn.com/b"])
        cluster_similar_results([r1, r2], embs)
        assert r1.cluster_id is None
        assert r2.cluster_id is None

    def test_threshold_env_override(self, monkeypatch):
        """A variável de ambiente CLUSTER_SIMILARITY_THRESHOLD deve ser respeitada."""
        monkeypatch.setenv("CLUSTER_SIMILARITY_THRESHOLD", "0.99")
        r1 = self._make_result("reddit", "https://reddit.com/x")
        r2 = self._make_result("hackernews", "https://hn.com/x")
        # Similaridade ~0.9 (perto mas abaixo do threshold alto de 0.99)
        embs = self._embeddings(
            ["https://reddit.com/x", "https://hn.com/x"],
            similar_pairs=[("https://reddit.com/x", "https://hn.com/x")],
        )
        cluster_similar_results([r1, r2], embs)
        # Com threshold 0.99, não deve agrupar (similaridade ~0.9 < 0.99)
        assert r1.cluster_id is None
        assert r2.cluster_id is None

    def test_three_sources_one_cluster(self):
        """Três fontes diferentes sobre o mesmo evento formam um único cluster."""
        r1 = self._make_result("reddit", "https://reddit.com/e")
        r2 = self._make_result("hackernews", "https://hn.com/e")
        r3 = self._make_result("news", "https://news.com/e")
        embs = self._embeddings(
            ["https://reddit.com/e", "https://hn.com/e", "https://news.com/e"],
            similar_pairs=[
                ("https://reddit.com/e", "https://hn.com/e"),
                ("https://reddit.com/e", "https://news.com/e"),
            ],
        )
        cluster_similar_results([r1, r2, r3], embs)
        assert r1.cluster_id == r2.cluster_id == r3.cluster_id
        assert r1.cluster_id is not None
        assert set(r1.corroborated_by) == {"hackernews", "news"}
        assert set(r2.corroborated_by) == {"reddit", "news"}
        assert set(r3.corroborated_by) == {"reddit", "hackernews"}

    def test_missing_embedding_skipped(self):
        """Resultados sem embedding disponível devem ser ignorados."""
        r1 = self._make_result("reddit", "https://reddit.com/m")
        r2 = self._make_result("hackernews", "https://hn.com/m")
        # r2 não tem embedding → não pode ser comparado
        embs = self._embeddings(["https://reddit.com/m"])
        cluster_similar_results([r1, r2], embs)
        assert r1.cluster_id is None
        assert r2.cluster_id is None
