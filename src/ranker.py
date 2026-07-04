"""Módulo de ranqueamento de qualidade dos resultados de busca.

Fornece a classe `QualityRanker` que pontua resultados brutos de cada
searcher usando heurísticas por fonte (GitHub, Reddit, HackerNews) e aplica
penalidades de desinformação via `MisinformationDetector`.

Ranking hibrido
----------------
Quando uma `query` e fornecida a `rank()`, o score deixa de ser apenas
heuristico e passa a combinar tres sinais — nenhum deles envolvendo LLM:

  1. Heuristicas por fonte (as mesmas de sempre: estrelas, upvotes, etc.).
  2. BM25 — relevancia lexical do titulo/descricao em relacao a query.
  3. Embeddings — similaridade semantica (via `SemanticReranker` existente).

A combinacao e feita por `src.ranking.hybrid_ranker.HybridRanker`, que
tambem faz pre-filtering: a etapa cara (embeddings) so roda sobre os
melhores candidatos por heuristica+BM25, nao sobre a lista inteira — isso
reduz o volume que chega as etapas de LLM mais caras do pipeline
(`ConfidenceScorerV2`, `ConflictDetector`, `PeerReviewAgent`, etc.), que
processam a saida deste ranker.

Quando nenhuma `query` e fornecida (compatibilidade com chamadores
existentes), o comportamento e identico ao anterior: apenas heuristica por
fonte + penalidade de desinformacao.
"""

import logging
import math
from datetime import datetime
from typing import Any

from src.clients.llm_client import LLMClient
from src.misinformation_detector import MisinformationDetector
from src.ranking.hybrid_ranker import HybridRanker, SemanticScorer
from src.types import RankedResult, SearchResult

logger = logging.getLogger(__name__)


class QualityRanker:
    """Ranqueador de qualidade de resultados de busca por fonte e recencia.

    Aplica heurísticas específicas por provedor (GitHub, Reddit, HackerNews)
    e penalidades de desinformação detectadas pelo `MisinformationDetector`.
    Quando uma query e informada, enriquece o ranking com BM25 + embeddings
    via `HybridRanker` (ver `src/ranking/hybrid_ranker.py`).
    """

    def __init__(
        self,
        llm_client: LLMClient = None,
        config: dict[str, Any] = None,
        semantic_scorer: SemanticScorer | None = None,
    ):
        self.llm = llm_client
        self.config = config or {}
        self.detector = MisinformationDetector()
        # Injetavel para testes/reuso; se None, um `SemanticReranker` proprio
        # e criado sob demanda (carregamento do modelo continua lazy — ver
        # `SemanticReranker._ensure_model` — entao nao ha custo se `rank()`
        # nunca for chamado com `query`).
        self._semantic_scorer = semantic_scorer
        self._hybrid_ranker: HybridRanker | None = None

    def _get_hybrid_ranker(self) -> HybridRanker:
        if self._hybrid_ranker is None:
            scorer = self._semantic_scorer
            if scorer is None:
                from src.search.semantic_reranker import SemanticReranker

                scorer = SemanticReranker()
            self._hybrid_ranker = HybridRanker(
                semantic_scorer=scorer,
                pre_filter_top_n=self.config.get("pre_filter_top_n", 50),
                weights=self.config.get("hybrid_weights"),
            )
        return self._hybrid_ranker

    def _recency_score(self, date_str: str) -> float:
        """Converte uma string de data em score de recencia (5.0-20.0).

        Args:
            date_str: String de data no formato ISO 8601 ou YYYY-MM-DD.

        Returns:
            float: Score de recencia entre 5.0 (antigo) e 20.0 (< 30 dias).
        """
        if not date_str:
            return 5.0
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                clean = date_str.replace("Z", "+00:00").split("+")[0]
                date = datetime.strptime(clean, fmt)
                days_ago = (datetime.now() - date).days
                if days_ago < 30:
                    return 20.0
                elif days_ago < 90:
                    return 15.0
                elif days_ago < 180:
                    return 10.0
                return 5.0
            except ValueError:
                continue
        return 5.0

    def _github_score(self, result: SearchResult) -> float:
        """Calcula score de qualidade para resultados do GitHub.

        Leva em conta estrelas, forks, recencia, licenca e linguagem.

        Args:
            result: Resultado bruto de busca com metricas do repositorio.

        Returns:
            float: Score entre 0 e 100.
        """
        m = result.metrics
        stars = m.get("stars", 0)
        forks = m.get("forks", 0)
        updated = m.get("updated_at", "")
        language = m.get("language")
        license_id = m.get("license")

        score = min(
            100,
            math.log10(stars + 1) * 15
            + math.log10(forks + 1) * 10
            + self._recency_score(updated)
            + (15 if license_id else 0)
            + (10 if language else 5),
        )
        return round(score, 2)

    def _reddit_score(self, result: SearchResult) -> float:
        """Calcula score de qualidade para resultados do Reddit.

        Considera upvotes, comentarios, recencia e relevancia do subreddit.

        Args:
            result: Resultado bruto de busca com metricas do post.

        Returns:
            float: Score entre 0 e 100.
        """
        m = result.metrics
        upvotes = m.get("upvotes", 0)
        comments = m.get("comments", 0)
        created = m.get("created_at", "")
        sub_rel = m.get("subreddit_relevance", 10)

        engagement_rate = comments / max(upvotes, 1) * 100
        score = min(
            100,
            math.log10(upvotes + 1) * 20
            + math.log10(comments + 1) * 15
            + self._recency_score(created)
            + sub_rel
            + min(engagement_rate, 20),
        )
        return round(score, 2)

    def _hn_score(self, result: SearchResult) -> float:
        """Calcula score de qualidade para resultados do Hacker News.

        Considera pontos, comentarios e recencia do item.

        Args:
            result: Resultado bruto de busca com metricas do item HN.

        Returns:
            float: Score entre 0 e 100.
        """
        m = result.metrics
        points = m.get("points", 0)
        comments = m.get("comments", 0)
        created = m.get("created_at", "")

        score = min(
            100,
            math.log10(points + 1) * 25
            + math.log10(comments + 1) * 15
            + self._recency_score(created)
            + (20 if m.get("url") else 10),
        )
        return round(score, 2)

    def _generic_score(self, result: SearchResult) -> float:
        """Retorna score neutro padrao para fontes sem heuristica especifica.

        Args:
            result: Resultado bruto de busca.

        Returns:
            float: Score fixo de 50.0.
        """
        return 50.0

    def _heuristic_score(self, result: SearchResult) -> float:
        """Despacha para a heuristica especifica da fonte do resultado."""
        if result.source == "github":
            return self._github_score(result)
        if result.source == "reddit":
            return self._reddit_score(result)
        if result.source == "hackernews":
            return self._hn_score(result)
        return self._generic_score(result)

    async def rank(
        self, results: list[SearchResult], query: str | None = None
    ) -> list[RankedResult]:
        """Ranqueia uma lista de resultados de busca por score de qualidade.

        Aplica heuristica especifica por fonte e penalidade de desinformacao
        quando a URL e detectada como suspeita pelo `MisinformationDetector`.
        Se `query` for informada, o score base heuristico e combinado com
        BM25 e embeddings via `HybridRanker` antes da penalidade de
        desinformacao ser aplicada — sem nenhuma chamada a LLM.

        Args:
            results: Lista de `SearchResult` brutos de qualquer searcher.
            query: Query original do usuario. Quando omitida, o
                comportamento e identico ao ranking puramente heuristico
                (compatibilidade retroativa com chamadores existentes).

        Returns:
            list[RankedResult]: Lista de resultados enriquecidos com score e
                breakdown de pontuacao, ordenada por score descendente.
        """
        if not results:
            return []

        heuristic_scores = [self._heuristic_score(r) for r in results]

        if query:
            try:
                hybrid = self._get_hybrid_ranker()
                hybrid_scored = await hybrid.rank(
                    results, query=query, heuristic_scores=heuristic_scores
                )
                base_entries = [
                    (
                        hr.result,
                        hr.final_score,
                        {
                            "heuristic_score": hr.heuristic_score,
                            "bm25_score": hr.bm25_score,
                            "embedding_score": hr.embedding_score,
                        },
                    )
                    for hr in hybrid_scored
                ]
            except Exception as e:
                logger.warning(
                    f"QualityRanker: hybrid ranking falhou ({e}); "
                    "usando apenas heuristica por fonte."
                )
                base_entries = [
                    (r, s, {"heuristic_score": s})
                    for r, s in zip(results, heuristic_scores)
                ]
        else:
            base_entries = [
                (r, s, {"heuristic_score": s})
                for r, s in zip(results, heuristic_scores)
            ]

        ranked = []
        for result, base_score, breakdown in base_entries:
            is_flagged, penalty, reason = self.detector.check_url(result.url)
            final_score = round(base_score * penalty, 2) if is_flagged else base_score

            ranked.append(
                RankedResult(
                    source=result.source,
                    title=result.title,
                    url=result.url,
                    description=result.description,
                    metrics=result.metrics,
                    raw=result.raw,
                    fetched_at=result.fetched_at,
                    score=final_score,
                    score_breakdown={
                        **breakdown,
                        "base_score": base_score,
                        "misinformation_penalty": penalty if is_flagged else 1.0,
                        "misinformation_reason": reason if is_flagged else "",
                    },
                )
            )

        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked
