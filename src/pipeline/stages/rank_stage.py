"""src/pipeline/stages/rank_stage.py

Stage independente do pipeline de pesquisa do Smart Research Agent (SRA)
responsável pelo ranking híbrido dos resultados coletados na busca (item 25
do plano de correções/melhorias): BM25 (lexical) + embeddings (semântico) +
reranking opcional via LLM/Cohere, com pre-filtering por heurísticas antes
das etapas caras e integração de sinais de feedback histórico.

CONTEXTO E TRANSPARÊNCIA SOBRE A ANÁLISE DO REPOSITÓRIO
---------------------------------------------------------
Mesma limitação de acesso já registrada no `expand_stage.py`: a API REST do
GitHub, o raw.githubusercontent.com e as páginas `/tree/...` estão
bloqueados neste ambiente; só consigo abrir páginas `blob` cujo link já
apareceu em conteúdo previamente carregado, e este repositório não está
indexado publicamente para busca. Tentei especificamente abrir
`src/research_score.py` (citado no SESSION_LOG.md como o módulo que hoje
concentra a lógica de scoring, decomposto em `_calculate_coverage`,
`_calculate_diversity`, `_calculate_quality`, `_calculate_reliability`,
`_calculate_recency`, `_calculate_conflicts`) e não consegui.

O que está **confirmado** pela leitura real de README.md e CHANGELOG.md do
repositório (não é suposição):
  * O projeto já lista `rank-bm25`, `cohere` e `sentence-transformers` como
    dependências instaláveis.
  * A versão 5.0.0 (CHANGELOG.md) já entregou "Hybrid Search Engine:
    Combined lexical (BM25) and dense embeddings vector search (ChromaDB)
    with Reciprocal Rank Fusion (RRF) and Cohere Reranking" — ou seja, RRF +
    Cohere rerank não são uma ideia nova para este projeto, já existem em
    alguma forma. Este stage foi desenhado para **consumir** esses
    componentes (BM25 index, embedding/vector store, cliente Cohere/LLM) via
    injeção de dependência e duck typing, e não para reimplementá-los do
    zero — evitando duplicar o que a v5.0.0 já fez.
  * `research_score.py` decompõe scoring em sub-critérios heurísticos
    (coverage, diversity, quality, reliability, recency, conflicts). Esse é
    exatamente o tipo de sinal barato que o "pre-filtering por heurísticas"
    deste stage deveria reaproveitar via `heuristic_scorer` injetado —
    novamente por duck typing, já que não tenho a assinatura exata.

Como não consegui ler os arquivos fonte, nenhum destes componentes (BM25,
embeddings, LLM/Cohere reranker, heurísticas, feedback store) é importado
diretamente por caminho fixo. Todos são injetados via `Protocol` estrutural,
com fallback interno de BM25 puro em Python (sem dependência externa) caso
nada seja injetado, para o stage nunca ficar sem funcionar. Recomendo colar
aqui o conteúdo real de `src/research_score.py`, do cliente de embeddings/
ChromaDB e do cliente LLM/Cohere usados hoje para eu trocar os adaptadores
defensivos por chamadas diretas.

Requisitos do item 25 atendidos:
  * Hybrid ranking (BM25 + embeddings + LLM) combinados via Reciprocal Rank
    Fusion (RRF), consistente com o que a v5.0.0 do projeto já usa.
  * Pre-filtering por heurísticas antes de qualquer chamada cara
    (embeddings/LLM), reduzindo custo.
  * Feedback signals integration: ajuste multiplicativo de score por sinais
    históricos (ex.: fonte confiável, domínio penalizado), plugável.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from src.types import RankedResult
try:
    import structlog

    logger = structlog.get_logger(__name__)
except ImportError:  # pragma: no cover - fallback se structlog não estiver instalado
    import logging

    logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Contratos estruturais (Protocol) — ver nota de transparência no topo do
# arquivo. Compatíveis por duck typing com classes reais de src/pipeline/,
# src/research_score.py e afins, sem depender delas existirem.
# --------------------------------------------------------------------------- #
@runtime_checkable
class PipelineContext(Protocol):
    """Contrato mínimo esperado do contexto compartilhado entre stages."""

    query: str
    intent: str | None
    search_results: list[Any]  # candidatos brutos vindos do search_stage (item 24)
    ranked_results: list[Any]


@runtime_checkable
class BM25Index(Protocol):
    """Contrato mínimo para um índice/scorer BM25 injetável (ex.: wrapper de
    `rank_bm25.BM25Okapi`, já presente nas dependências do projeto).
    """

    def score(self, query: str, documents: list[str]) -> list[float]: ...


@runtime_checkable
class EmbeddingRanker(Protocol):
    """Contrato mínimo para um ranker semântico injetável (ex.: wrapper do
    vector store ChromaDB / `sentence-transformers` já usado no projeto).
    """

    def rank(self, query: str, candidates: list[Any]) -> list[float]: ...


@runtime_checkable
class LLMReranker(Protocol):
    """Contrato mínimo para reranking caro (Cohere rerank ou LLM judge),
    aplicado apenas ao top-K pós-RRF para controle de custo.
    """

    def rerank(self, query: str, candidates: list[Any], top_n: int) -> Any: ...


@runtime_checkable
class HeuristicScorer(Protocol):
    """Contrato mínimo para o pre-filter heurístico barato (ex.: wrapper de
    `src/research_score.py`, reaproveitando sub-critérios como recency e
    domain authority sem precisar de LLM ou embeddings).
    """

    def score(self, candidate: Any) -> float: ...


@runtime_checkable
class FeedbackStore(Protocol):
    """Contrato mínimo para sinais de feedback histórico (ex.: base para o
    `learned_ranker` do item 38, ainda não implementado no repositório)."""

    def get_boost(self, candidate: Any) -> float: ...


@dataclass
class RankStageResult:
    """Resultado estruturado produzido por este stage (auditável/testável)."""

    ranked_results: list[Any]
    n_input: int
    n_after_prefilter: int
    n_reranked_by_llm: int
    signals_used: list[str]
    duration_ms: float
    error: str | None = None


# --------------------------------------------------------------------------- #
# Fallback leve de BM25 puro em Python — usado apenas se nenhum BM25Index for
# injetado, para o stage nunca ficar sem sinal lexical mesmo sem `rank_bm25`
# instalado no ambiente de teste.
# --------------------------------------------------------------------------- #
class _NaiveBM25:
    _TOKEN_RE = re.compile(r"[a-zA-Z0-9À-ÿ]+")

    def _tokenize(self, text: str) -> list[str]:
        return self._TOKEN_RE.findall(text.lower())

    def score(self, query: str, documents: list[str]) -> list[float]:
        query_terms = self._tokenize(query)
        if not query_terms or not documents:
            return [0.0] * len(documents)

        doc_tokens = [self._tokenize(doc) for doc in documents]
        doc_lengths = [len(tokens) for tokens in doc_tokens]
        avg_len = (sum(doc_lengths) / len(doc_lengths)) if doc_lengths else 1.0
        n_docs = len(documents)

        df = Counter()
        for tokens in doc_tokens:
            for term in set(tokens):
                df[term] += 1

        k1, b = 1.5, 0.75
        scores: list[float] = []
        for tokens, length in zip(doc_tokens, doc_lengths):
            tf = Counter(tokens)
            score = 0.0
            for term in query_terms:
                if tf[term] == 0:
                    continue
                idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                numerator = tf[term] * (k1 + 1)
                denominator = tf[term] + k1 * (1 - b + b * length / max(avg_len, 1e-9))
                score += idf * numerator / max(denominator, 1e-9)
            scores.append(score)
        return scores


def _extract_text(candidate: Any) -> str:
    """Extrai texto representativo de um candidato para BM25, aceitando
    dict, dataclass ou objeto genérico (assinatura real desconhecida).
    """
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        return " ".join(
            str(candidate.get(k, ""))
            for k in ("title", "text", "snippet", "content", "summary")
            if candidate.get(k)
        )
    parts = []
    for attr in ("title", "text", "snippet", "content", "summary"):
        value = getattr(candidate, attr, None)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _reciprocal_rank_fusion(
    ranked_lists: list[list[int]], k: int = 60, weights: list[float] | None = None
) -> dict[int, float]:
    """RRF padrão: score(d) = soma_i[ w_i / (k + rank_i(d)) ].

    Consistente com a técnica de RRF já usada pelo projeto desde a v5.0.0
    (ver CHANGELOG.md), aqui generalizada para aceitar pesos por sinal.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    fused: dict[int, float] = {}
    for ranked_list, weight in zip(ranked_lists, weights):
        for rank, idx in enumerate(ranked_list):
            fused[idx] = fused.get(idx, 0.0) + weight / (k + rank + 1)
    return fused


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class RankStage:
    """Stage do pipeline responsável pelo ranking híbrido (item 25).

    Pipeline interno:
      1. Pre-filter heurístico barato (reduz N antes de embeddings/LLM).
      2. Scoring lexical (BM25) e semântico (embeddings) em paralelo.
      3. Fusão via RRF ponderado.
      4. Reranking caro (LLM/Cohere) só no top-K pós-RRF.
      5. Ajuste final por sinais de feedback histórico.

    Todas as etapas caras/opcionais degradam graciosamente se o componente
    correspondente não estiver disponível ou falhar (regra de ouro nº5 do
    próprio guia de implementação do projeto: nenhum componente pode
    derrubar o pipeline).

    Parameters
    ----------
    bm25_index, embedding_ranker, llm_reranker, heuristic_scorer,
    feedback_store:
        Componentes injetáveis (duck typing). Todos opcionais.
    prefilter_keep_ratio:
        Fração dos candidatos mantida após o pre-filter heurístico
        (ex.: 0.6 mantém os 60% melhores por heurística antes de
        embeddings/LLM).
    prefilter_min_candidates:
        Piso de candidatos mantidos pelo pre-filter, mesmo que
        `prefilter_keep_ratio` resultasse em menos.
    llm_rerank_top_k:
        Quantos candidatos pós-RRF são enviados ao reranker caro.
    rrf_k:
        Constante `k` da fórmula RRF (60 é o valor de referência mais comum
        na literatura e o usado por padrão em Elasticsearch/OpenSearch).
    signal_weights:
        Pesos relativos de BM25 e embeddings na fusão RRF.
    """

    name = "rank"

    def __init__(
        self,
        bm25_index: BM25Index | None = None,
        embedding_ranker: EmbeddingRanker | None = None,
        llm_reranker: LLMReranker | None = None,
        heuristic_scorer: HeuristicScorer | None = None,
        feedback_store: FeedbackStore | None = None,
        prefilter_keep_ratio: float = 0.6,
        prefilter_min_candidates: int = 10,
        llm_rerank_top_k: int = 10,
        rrf_k: int = 60,
        signal_weights: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        self._bm25_index: BM25Index = bm25_index or _NaiveBM25()
        self._embedding_ranker = embedding_ranker
        self._llm_reranker = llm_reranker
        self._heuristic_scorer = heuristic_scorer
        self._feedback_store = feedback_store
        self._prefilter_keep_ratio = prefilter_keep_ratio
        self._prefilter_min_candidates = prefilter_min_candidates
        self._llm_rerank_top_k = llm_rerank_top_k
        self._rrf_k = rrf_k
        self._signal_weights = signal_weights

    async def run(self, context: PipelineContext) -> PipelineContext:
        start = time.perf_counter()
        query = context.query
        candidates = list(getattr(context, "search_results", None) or getattr(context, "raw_results", None) or [])
        signals_used: list[str] = []

        if not candidates:
            context.ranked_results = []
            logger.info("rank_stage.empty_input", query=query)
            return context

        try:
            prefiltered = self._prefilter(candidates)
            if len(prefiltered) < len(candidates):
                signals_used.append("heuristic_prefilter")

            fused_order = await self._hybrid_rank(query, prefiltered, signals_used)

            reranked = await self._llm_rerank(query, fused_order, signals_used)

            final_order = self._apply_feedback(reranked, signals_used)

            context.ranked_results = final_order
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "rank_stage.done",
                query=query,
                n_input=len(candidates),
                n_after_prefilter=len(prefiltered),
                n_output=len(final_order),
                signals_used=signals_used,
                duration_ms=duration_ms,
            )
            return context

        except Exception as exc:  # noqa: BLE001 - fallback obrigatório (regra de ouro nº5)
            logger.warning(
                "rank_stage.fallback_original_order",
                query=query,
                error=str(exc),
                exc_info=exc,
            )
            ranked_results = []
            for i, c in enumerate(candidates):
                norm_score = 100.0 - (i * (100.0 / len(candidates))) if len(candidates) > 1 else 100.0
                if isinstance(c, RankedResult):
                    c.score = norm_score
                    ranked_results.append(c)
                else:
                    data = c.model_dump() if hasattr(c, "model_dump") else c if isinstance(c, dict) else {}
                    ranked_results.append(RankedResult(**data, score=norm_score))
            context.ranked_results = ranked_results
            return context

    # ------------------------------------------------------------------ #
    # 1. Pre-filtering por heurísticas
    # ------------------------------------------------------------------ #
    def _prefilter(self, candidates: list[Any]) -> list[Any]:
        if self._heuristic_scorer is None or len(candidates) <= self._prefilter_min_candidates:
            return candidates

        try:
            scored = [(self._heuristic_scorer.score(c), c) for c in candidates]
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stage.heuristic_scorer_failed", error=str(exc))
            return candidates

        scored.sort(key=lambda pair: pair[0], reverse=True)
        keep_n = max(
            self._prefilter_min_candidates,
            int(len(candidates) * self._prefilter_keep_ratio),
        )
        return [c for _, c in scored[:keep_n]]

    # ------------------------------------------------------------------ #
    # 2-3. Hybrid ranking (BM25 + embeddings) via RRF
    # ------------------------------------------------------------------ #
    async def _hybrid_rank(
        self, query: str, candidates: list[Any], signals_used: list[str]
    ) -> list[Any]:
        if not candidates:
            return []

        ranked_lists: list[list[int]] = []
        weights: list[float] = []

        bm25_ranks = await self._safe_bm25_ranks(query, candidates)
        if bm25_ranks is not None:
            ranked_lists.append(bm25_ranks)
            weights.append(self._signal_weights[0])
            signals_used.append("bm25")

        embedding_ranks = await self._safe_embedding_ranks(query, candidates)
        if embedding_ranks is not None:
            ranked_lists.append(embedding_ranks)
            weights.append(self._signal_weights[1])
            signals_used.append("embeddings")

        if not ranked_lists:
            # nenhum sinal disponível: preserva ordem original mas converte para RankedResult
            ranked_results = []
            for i, c in enumerate(candidates):
                norm_score = 100.0 - (i * (100.0 / len(candidates))) if len(candidates) > 1 else 100.0
                if isinstance(c, RankedResult):
                    c.score = norm_score
                    ranked_results.append(c)
                else:
                    data = c.model_dump() if hasattr(c, "model_dump") else c if isinstance(c, dict) else {}
                    ranked_results.append(RankedResult(**data, score=norm_score))
            return ranked_results

        fused_scores = _reciprocal_rank_fusion(ranked_lists, k=self._rrf_k, weights=weights)
        ordered_indices = sorted(
            fused_scores.keys(), key=lambda i: fused_scores[i], reverse=True
        )

        max_fused = max(fused_scores.values()) if fused_scores else 1.0
        ranked_results = []
        for idx in ordered_indices:
            c = candidates[idx]
            norm_score = (fused_scores[idx] / max_fused) * 100.0 if max_fused > 0 else 0.0
            if isinstance(c, RankedResult):
                c.score = norm_score
                ranked_results.append(c)
            else:
                data = c.model_dump() if hasattr(c, "model_dump") else c if isinstance(c, dict) else {}
                ranked_result = RankedResult(
                    **data,
                    score=norm_score,
                    score_breakdown={"rrf_score": fused_scores[idx]}
                )
                ranked_results.append(ranked_result)
        return ranked_results

    async def _safe_bm25_ranks(self, query: str, candidates: list[Any]) -> list[int] | None:
        try:
            texts = [_extract_text(c) for c in candidates]
            scores = await _maybe_await(self._bm25_index.score(query, texts))
            return sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stage.bm25_failed", error=str(exc))
            return None

    async def _safe_embedding_ranks(
        self, query: str, candidates: list[Any]
    ) -> list[int] | None:
        if self._embedding_ranker is None:
            return None
        try:
            scores = await _maybe_await(self._embedding_ranker.rank(query, candidates))
            return sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stage.embedding_ranker_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------ #
    # 4. Reranking caro (LLM/Cohere), só no top-K
    # ------------------------------------------------------------------ #
    async def _llm_rerank(
        self, query: str, ordered_candidates: list[Any], signals_used: list[str]
    ) -> list[Any]:
        if self._llm_reranker is None or not ordered_candidates:
            return ordered_candidates

        top_k = ordered_candidates[: self._llm_rerank_top_k]
        rest = ordered_candidates[self._llm_rerank_top_k :]

        try:
            reranked_top_k = await _maybe_await(
                self._llm_reranker.rerank(query, top_k, top_n=len(top_k))
            )
            reranked_top_k = list(reranked_top_k) if reranked_top_k else top_k
            signals_used.append("llm_rerank")
            return reranked_top_k + rest
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stage.llm_rerank_failed", error=str(exc))
            return ordered_candidates

    # ------------------------------------------------------------------ #
    # 5. Feedback signals integration
    # ------------------------------------------------------------------ #
    def _apply_feedback(self, candidates: list[Any], signals_used: list[str]) -> list[Any]:
        if self._feedback_store is None or not candidates:
            return candidates

        try:
            boosted = [
                (self._feedback_store.get_boost(c) or 1.0, position, c)
                for position, c in enumerate(candidates)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stage.feedback_store_failed", error=str(exc))
            return candidates

        # ordena por boost desc, mantendo a ordem relativa original (posição)
        # como critério de desempate estável
        boosted.sort(key=lambda item: (-item[0], item[1]))
        signals_used.append("feedback")
        return [c for _, _, c in boosted]


__all__ = [
    "RankStage",
    "RankStageResult",
    "PipelineContext",
    "BM25Index",
    "EmbeddingRanker",
    "LLMReranker",
    "HeuristicScorer",
    "FeedbackStore",
]
