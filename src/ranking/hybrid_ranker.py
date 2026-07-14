"""Hybrid Ranker — Ranking híbrido (BM25 + embeddings + LLM) para o SRA.

Combina múltiplos sinais de relevância para ranquear resultados de busca:
  1. BM25: relevância lexical entre query e conteúdo
  2. Embeddings: similaridade semântica via sentence-transformers
  3. Heurísticas: freshness, domain authority, source-specific signals
  4. LLM: re-ranking apenas do top-K candidatos (economia de tokens)

Arquitetura:
  - Pre-filtering: heurísticas rápidas eliminam candidatos obvios
  - BM25 + embeddings: scoring híbrido nos candidatos restantes
  - Top-K selection: seleciona os melhores para LLM re-ranking
  - LLM re-rank: análise profunda apenas nos top-K (default 20)
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.config_loader import load_scoring_weights
from src.types import RankedResult, SearchResult

logger = logging.getLogger("ranking.hybrid_ranker")


# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_LLM_TOP_K: int = 20
DEFAULT_PRE_FILTER_THRESHOLD: float = 0.15
DEFAULT_BM25_WEIGHT: float = 0.30
DEFAULT_EMBEDDING_WEIGHT: float = 0.30
DEFAULT_HEURISTIC_WEIGHT: float = 0.25
DEFAULT_LLM_WEIGHT: float = 0.15

# Domain authority scores (0-1)
DOMAIN_AUTHORITY: Dict[str, float] = {
    "github.com": 0.95,
    "arxiv.org": 0.92,
    "stackoverflow.com": 0.90,
    "news.ycombinator.com": 0.85,
    "reddit.com": 0.75,
    "producthunt.com": 0.80,
    "medium.com": 0.70,
    "dev.to": 0.75,
    "docs.python.org": 0.95,
    "developer.mozilla.org": 0.95,
    "wikipedia.org": 0.88,
    "default": 0.50,
}

# Source-specific freshness halflife (days)
FRESHNESS_HALFLIFE: Dict[str, float] = {
    "github": 90.0,
    "reddit": 7.0,
    "hackernews": 14.0,
    "arxiv": 365.0,
    "stackoverflow": 180.0,
    "rss": 3.0,
    # Fontes de notícia (Plano Parte 4 — Fase 2): meia-vida curta,
    # conteúdo de minuto a minuto decai rápido.
    "gdelt": 0.5,  # 12 horas
    "google_news_rss": 0.5,  # 12 horas
    "newsapi_org": 0.5,  # 12 horas
    "bluesky": 0.25,  # 6 horas — rede social decai super rápido
    "mastodon_social": 0.25,  # 6 horas
    "default": 30.0,
}


# ── Configuração ─────────────────────────────────────────────────────────────


@dataclass
class HybridRankerConfig:
    """Configuração fina do HybridRanker.

    Attributes:
        bm25_weight: Peso do score BM25 (0-1).
        embedding_weight: Peso da similaridade de embeddings (0-1).
        heuristic_weight: Peso das heurísticas (freshness, authority) (0-1).
        llm_weight: Peso do re-ranking LLM (0-1).
        llm_top_k: Quantos top candidatos enviar para o LLM re-rank.
        pre_filter_threshold: Score mínimo para passar do pre-filtering.
        bm25_k1: Parâmetro k1 do BM25 (controle de saturação de term frequency).
        bm25_b: Parâmetro b do BM25 (controle de normalização por document length).
        embedding_model: Nome do modelo sentence-transformers.
        embedding_device: Device para embeddings ('cpu', 'cuda', 'mps').
        max_results: Máximo de resultados retornados.
        enable_llm_rerank: Se True, usa LLM para re-ranking do top-K.
        enable_pre_filter: Se True, aplica pre-filtering por heurísticas.
        freshness_boost_days: Dias para considerar "recente" (boost máximo).
    """

    bm25_weight: float = DEFAULT_BM25_WEIGHT
    embedding_weight: float = DEFAULT_EMBEDDING_WEIGHT
    heuristic_weight: float = DEFAULT_HEURISTIC_WEIGHT
    llm_weight: float = DEFAULT_LLM_WEIGHT
    llm_top_k: int = DEFAULT_LLM_TOP_K
    pre_filter_threshold: float = DEFAULT_PRE_FILTER_THRESHOLD
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    max_results: int = 100
    enable_llm_rerank: bool = True
    enable_pre_filter: bool = True
    freshness_boost_days: float = 30.0

    def __post_init__(self) -> None:
        self._apply_yaml_weights()
        total = (
            self.bm25_weight
            + self.embedding_weight
            + self.heuristic_weight
            + self.llm_weight
        )
        if abs(total - 1.0) > 0.01:
            logger.warning(
                f"Soma dos pesos = {total:.2f} (esperado 1.0). Normalizando."
            )
            self.bm25_weight /= total
            self.embedding_weight /= total
            self.heuristic_weight /= total
            self.llm_weight /= total

    def _apply_yaml_weights(self) -> None:
        """Aplica pesos de ``config/scoring_weights.yaml`` sobre os defaults.

        Lê a seção ``weights:`` do YAML (carregada com cache por
        ``load_scoring_weights``). Cada peso só é sobrescrito se vier como número
        e for diferente do valor atual — preservando os defaults hardcoded
        quando o YAML está ausente, vazio ou com tipos inválidos. Em caso de
        sucesso, loga os valores efetivamente aplicados (prova de vida do
        Bloco 2: editar o YAML reflete no log do ranker sem tocar em Python).
        """
        weights = load_scoring_weights().get("weights")
        if not isinstance(weights, dict):
            return  # YAML ausente/sem seção weights -> mantém defaults

        overwritten: dict[str, float] = {}
        mapping: tuple[tuple[str, str, float], ...] = (
            ("bm25", "bm25_weight", DEFAULT_BM25_WEIGHT),
            ("embedding", "embedding_weight", DEFAULT_EMBEDDING_WEIGHT),
            ("heuristic", "heuristic_weight", DEFAULT_HEURISTIC_WEIGHT),
            ("llm", "llm_weight", DEFAULT_LLM_WEIGHT),
        )
        for yaml_key, attr, default_value in mapping:
            value = weights.get(yaml_key, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value != default_value:
                    overwritten[yaml_key] = float(value)
                setattr(self, attr, float(value))

        if overwritten:
            logger.info(
                "HybridRankerConfig: pesos aplicados de scoring_weights.yaml: %s",
                {k: round(v, 4) for k, v in overwritten.items()},
            )


# ── Dataclasses internas ─────────────────────────────────────────────────────


@dataclass
class ScoreBreakdown:
    """Breakdown detalhado do score de um resultado."""

    bm25_score: float = 0.0
    embedding_score: float = 0.0
    heuristic_score: float = 0.0
    llm_score: float = 0.0
    freshness_score: float = 0.0
    authority_score: float = 0.0
    source_boost: float = 0.0
    final_score: float = 0.0


@dataclass
class Candidate:
    """Candidato intermediário no pipeline de ranking."""

    result: SearchResult
    breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    pre_filter_score: float = 0.0
    hybrid_score: float = 0.0
    llm_score: Optional[float] = None
    rank: int = 0


# ── HybridRanker ────────────────────────────────────────────────────────────


class HybridRanker:
    """Ranqueador híbrido que combina BM25, embeddings, heurísticas e LLM.

    Pipeline:
      1. Pre-filtering: heurísticas rápidas (source quality, freshness mínima)
      2. BM25 scoring: relevância lexical query ↔ documento
      3. Embedding scoring: similaridade semântica via sentence-transformers
      4. Heuristic scoring: freshness + domain authority + source signals
      5. Hybrid fusion: combinação ponderada dos scores
      6. Top-K selection: seleciona os melhores para LLM
      7. LLM re-rank: análise profunda apenas no top-K (economia de tokens)
      8. Final ranking: merge do hybrid score + LLM score

    Args:
        llm_client: Cliente LLM para re-ranking (pode ser None se enable_llm_rerank=False).
        config: Configuração do ranker.
        embedding_fn: Função de embedding customizada (None = carrega sentence-transformers).
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        config: Optional[HybridRankerConfig] = None,
        embedding_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
        # Argumentos de compatibilidade
        semantic_scorer: Optional[Any] = None,
        pre_filter_top_n: int = 50,
        weights: Optional[dict[str, float]] = None,
    ):
        self.config = config or HybridRankerConfig()
        self.llm = llm_client
        self._embedding_fn = embedding_fn
        self._embedding_model: Optional[Any] = None
        self._bm25_ready: bool = False

        # Compatibilidade retroativa
        self.semantic_scorer = semantic_scorer
        self.pre_filter_top_n = pre_filter_top_n
        self.weights = weights or {
            "heuristic": self.config.heuristic_weight,
            "bm25": self.config.bm25_weight,
            "embedding": self.config.embedding_weight,
            "llm": self.config.llm_weight,
        }

    # ── API Pública ─────────────────────────────────────────────────────────

    async def rank(
        self,
        results: List[SearchResult],
        query: str,
        heuristic_scores: Optional[List[float]] = None,
    ) -> List[Any]:
        """Ranqueia resultados usando pipeline híbrido completo.

        Se `heuristic_scores` for passado, executa o modo de compatibilidade anterior.
        """
        if heuristic_scores is not None:
            return await self._rank_compatibility(results, query, heuristic_scores)

        if not results:
            return []

        logger.info(
            f"HybridRanker: ranqueando {len(results)} resultados para query: {query[:60]}"
        )

        # 1. Pre-filtering
        candidates = self._pre_filter(results, query)
        if not candidates:
            logger.warning(
                "HybridRanker: todos os resultados eliminados no pre-filtering"
            )
            return []
        logger.debug(
            f"HybridRanker: {len(candidates)}/{len(results)} passaram do pre-filtering"
        )

        # 2. BM25 scoring
        self._score_bm25(candidates, query)

        # 3. Embedding scoring
        await self._score_embeddings(candidates, query)

        # 4. Heuristic scoring
        self._score_heuristics(candidates, query)

        # 5. Hybrid fusion
        self._compute_hybrid_scores(candidates)

        # Ordena por hybrid score
        candidates.sort(key=lambda c: c.hybrid_score, reverse=True)

        # 6. Top-K selection para LLM
        top_k = candidates[: self.config.llm_top_k]

        # 7. LLM re-ranking (apenas top-K)
        if self.config.enable_llm_rerank and self.llm is not None:
            await self._score_llm(top_k, query)
            self._compute_final_scores(top_k)
            # Reordena top-K
            top_k.sort(key=lambda c: c.breakdown.final_score, reverse=True)

        # 8. Monta resultado final
        final_candidates = top_k + candidates[self.config.llm_top_k :]
        final_candidates.sort(key=lambda c: c.breakdown.final_score, reverse=True)

        ranked = self._to_ranked_results(final_candidates)
        logger.info(
            f"HybridRanker: {len(ranked)} resultados ranqueados (top score: {ranked[0].score:.3f})"
        )
        return ranked[: self.config.max_results]

    async def _rank_compatibility(
        self,
        results: List[SearchResult],
        query: str,
        heuristic_scores: List[float],
    ) -> List[Any]:
        if len(results) != len(heuristic_scores):
            raise ValueError("Mismatched lengths between results and heuristic_scores")

        # 1. BM25 scoring
        corpus = [
            self._tokenize(f"{r.title or ''} {r.description or ''}") for r in results
        ]
        bm25 = BM25(corpus)
        query_tokens = self._tokenize(query)
        bm25_scores = bm25.scores_for_query(query_tokens)

        # Normalizar scores
        normalized_bm25 = _normalize(bm25_scores)
        normalized_heuristics = _normalize(heuristic_scores)

        # 2. Pre-filtering & Semantics
        candidates = []
        for i, r in enumerate(results):
            pf_score = normalized_heuristics[i] + normalized_bm25[i]
            candidates.append(
                {
                    "index": i,
                    "result": r,
                    "pf_score": pf_score,
                    "bm25_score": bm25_scores[i],
                    "normalized_bm25": normalized_bm25[i],
                    "heuristic_score": heuristic_scores[i],
                    "normalized_heuristic": normalized_heuristics[i],
                    "embedding_score": None,
                }
            )

        # Ordenar pelo pre-filter score para selecionar top_n
        candidates.sort(key=lambda c: c["pf_score"], reverse=True)

        # Chamar semantic scorer apenas para os top_n candidatos
        top_candidates = candidates[: self.pre_filter_top_n]
        if self.semantic_scorer is not None and top_candidates:
            results_to_rerank = []
            for c in top_candidates:
                r = c["result"]
                results_to_rerank.append(
                    {
                        "title": r.title,
                        "description": r.description,
                        "url": r.url,
                        "source": r.source,
                        "metrics": r.metrics,
                    }
                )
            try:
                reranked = await self.semantic_scorer.rerank(query, results_to_rerank)
                for c, rr in zip(top_candidates, reranked):
                    # Se o reranked for um dict, pega com .get(); se for um objeto, pega com getattr() ou .get() se suportar dict
                    if isinstance(rr, dict):
                        c["embedding_score"] = rr.get("_semantic_score")
                    else:
                        c["embedding_score"] = getattr(
                            rr, "_semantic_score", getattr(rr, "embedding_score", None)
                        )
            except Exception as e:
                logger.warning(f"Semantic scorer failed in compatibility mode: {e}")

        # Calcular scores finais e empacotar em HybridRankResultCompat
        w_heuristic = self.weights.get("heuristic", 0.4)
        w_bm25 = self.weights.get("bm25", 0.3)
        w_embedding = self.weights.get("embedding", 0.3)

        output = []
        for c in candidates:
            h_s = float(c["normalized_heuristic"])
            b_s = float(c["normalized_bm25"])
            # embedding_score pode ser None (sem reranking semântico) — preservamos
            # o valor original (Optional[float]) para o HybridRankResultCompat e
            # usamos uma cópia numérica só para a aritmética do score.
            e_s: float | None = c["embedding_score"]

            if e_s is None:
                denom = w_heuristic + w_bm25
                if denom > 0:
                    final_score = (w_heuristic * h_s + w_bm25 * b_s) / denom
                else:
                    final_score = 0.0
            else:
                e_s = float(e_s)
                final_score = w_heuristic * h_s + w_bm25 * b_s + w_embedding * e_s

            # final_score é ponderado em escala 0-1, convertemos para range 0-100 para condizer com heuristic_scores
            # mas garante que o valor absoluto não ultrapasse o teto
            final_scaled = final_score * 100.0
            output.append(
                HybridRankResultCompat(
                    result=c["result"],
                    final_score=final_scaled,
                    heuristic_score=c["heuristic_score"],
                    bm25_score=c["bm25_score"],
                    embedding_score=e_s,
                )
            )

        output.sort(key=lambda o: o.final_score, reverse=True)
        return output

    # ── Pre-filtering ─────────────────────────────────────────────────────────

    def _pre_filter(self, results: List[SearchResult], query: str) -> List[Candidate]:
        """Elimina candidatos obvios com heurísticas rápidas (O(n)).

        Critérios:
          - Conteúdo muito curto (< 50 chars)
          - URLs suspeitas (spam domains)
          - Score de source muito baixo
          - Duplicatas exatas de URL
        """
        candidates: List[Candidate] = []
        seen_urls: set = set()
        query_terms = set(query.lower().split())

        for result in results:
            # Elimina duplicatas
            url_key = result.url.split("?")[0].rstrip("/")
            if url_key in seen_urls:
                continue
            seen_urls.add(url_key)

            # Elimina conteúdo muito curto
            content = (result.description or "") + " " + (result.title or "")
            if len(content.strip()) < 50:
                continue

            # Elimina URLs suspeitas
            if self._is_suspicious_url(result.url):
                continue

            # Score rápido de overlap lexical
            content_terms = set(content.lower().split())
            overlap = len(query_terms & content_terms) / max(len(query_terms), 1)

            # Score de source quality
            source_score = self._get_source_quality(result.source)

            pre_filter_score = overlap * 0.6 + source_score * 0.4

            if (
                self.config.enable_pre_filter
                and pre_filter_score < self.config.pre_filter_threshold
            ):
                continue

            candidates.append(
                Candidate(
                    result=result,
                    pre_filter_score=pre_filter_score,
                )
            )

        return candidates

    # ── BM25 Scoring ──────────────────────────────────────────────────────────

    def _score_bm25(self, candidates: List[Candidate], query: str) -> None:
        """Calcula score BM25 para cada candidato.

        BM25 clássico com k1 e b configuráveis.
        """
        if not candidates:
            return

        # Tokenização simples (pode ser melhorada com stemming)
        query_terms = self._tokenize(query)
        if not query_terms:
            return

        # Documentos
        docs: List[List[str]] = []
        for c in candidates:
            text = f"{c.result.title or ''} {c.result.description or ''}"
            docs.append(self._tokenize(text))

        # Estatísticas da coleção
        N = len(docs)
        avgdl = sum(len(d) for d in docs) / N if N > 0 else 1.0

        # Document frequency
        df: Dict[str, int] = {}
        for doc in docs:
            seen = set(doc)
            for term in seen:
                df[term] = df.get(term, 0) + 1

        k1 = self.config.bm25_k1
        b = self.config.bm25_b

        for i, candidate in enumerate(candidates):
            doc = docs[i]
            doc_len = len(doc)
            score = 0.0

            for term in query_terms:
                tf = doc.count(term)
                if tf == 0:
                    continue

                idf = math.log(
                    (N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1.0
                )
                tf_component = (tf * (k1 + 1)) / (
                    tf + k1 * (1 - b + b * (doc_len / avgdl))
                )
                score += idf * tf_component

            # Normaliza para 0-1 (aproximado)
            max_possible = len(query_terms) * math.log(N + 1) * (k1 + 1) / k1
            normalized = score / max(max_possible, 1.0)
            candidate.breakdown.bm25_score = max(0.0, min(1.0, normalized))

    # ── Embedding Scoring ───────────────────────────────────────────────────

    async def _score_embeddings(self, candidates: List[Candidate], query: str) -> None:
        """Calcula similaridade de embeddings entre query e documentos."""
        if not candidates:
            return

        texts = [
            f"{c.result.title or ''}. {c.result.description or ''}" for c in candidates
        ]

        try:
            embeddings = await self._get_embeddings([query] + texts)
            if not embeddings or len(embeddings) < 2:
                return

            query_embedding = embeddings[0]
            doc_embeddings = embeddings[1:]

            for i, candidate in enumerate(candidates):
                if i < len(doc_embeddings):
                    similarity = self._cosine_similarity(
                        query_embedding, doc_embeddings[i]
                    )
                    candidate.breakdown.embedding_score = max(
                        0.0, min(1.0, (similarity + 1) / 2)
                    )

        except Exception as e:
            logger.warning(f"Embedding scoring falhou: {e}. Usando fallback lexical.")
            # Fallback: usa overlap lexical como proxy
            query_terms = set(query.lower().split())
            for candidate in candidates:
                text = f"{candidate.result.title or ''} {candidate.result.description or ''}".lower()
                text_terms = set(text.split())
                overlap = len(query_terms & text_terms) / max(len(query_terms), 1)
                candidate.breakdown.embedding_score = overlap

    async def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Obtém embeddings para uma lista de textos."""
        if self._embedding_fn is not None:
            return self._embedding_fn(texts)

        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._embedding_model = SentenceTransformer(
                    self.config.embedding_model,
                    device=self.config.embedding_device,
                )
                logger.info(
                    f"Modelo de embeddings carregado: {self.config.embedding_model}"
                )
            except ImportError:
                logger.warning(
                    "sentence-transformers não instalado. Embeddings desabilitados."
                )
                return []

        # Executa em thread para não bloquear o event loop
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            self._embedding_model.encode,
            texts,
        )
        return (
            embeddings.tolist() if hasattr(embeddings, "tolist") else list(embeddings)
        )

    # ── Heuristic Scoring ───────────────────────────────────────────────────

    def _score_heuristics(self, candidates: List[Candidate], query: str) -> None:
        """Calcula heurísticas: freshness, domain authority, source signals."""
        now = datetime.now(timezone.utc)

        for candidate in candidates:
            result = candidate.result

            # Freshness score
            freshness = self._compute_freshness(result, now)
            candidate.breakdown.freshness_score = freshness

            # Domain authority
            authority = self._compute_authority(result.url)
            candidate.breakdown.authority_score = authority

            # Source-specific boost
            source_boost = self._get_source_quality(result.source)
            candidate.breakdown.source_boost = source_boost

            # Heuristic composite
            candidate.breakdown.heuristic_score = (
                freshness * 0.40 + authority * 0.35 + source_boost * 0.25
            )

    def _compute_freshness(self, result: SearchResult, now: datetime) -> float:
        """Computa score de freshness (0-1) baseado na idade do resultado.

        Usa ``published_at`` quando disponível, com fallback para
        ``fetched_at``. O tratamento é robusto a timezone (aware vs naive),
        evitando ``TypeError: can't subtract offset-naive and offset-aware
        datetimes``.
        """
        published_at = getattr(result, "published_at", None)
        reference_time = published_at or getattr(result, "fetched_at", None)
        if reference_time is None:
            return 0.5

        try:
            if isinstance(reference_time, str):
                # Caso venha serializado como string do JSON (ISO-8601)
                reference_time = datetime.fromisoformat(
                    reference_time.replace("Z", "+00:00")
                )

            # Conformidade matemática de timezone: normalizar SEMPRE para
            # aware-UTC antes de subtrair. Um datetime naive é interpretado
            # como UTC (convenção do SRA — `fetched_at`/`published_at` são
            # gravados em UTC), e não como hora local do servidor. Isto elimina
            # o desvio pelo offset local que ocorria ao comparar uma referência
            # naive contra `datetime.now()` (hora local) enquanto referências
            # aware eram comparadas contra `now` (UTC).
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            now_cmp = (
                now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            )

            age_days = (now_cmp - reference_time).total_seconds() / 86400.0
        except Exception:
            return 0.5

        halflife = FRESHNESS_HALFLIFE.get(result.source, FRESHNESS_HALFLIFE["default"])
        score = math.exp(-age_days / halflife)

        if age_days <= self.config.freshness_boost_days:
            score = min(1.0, score * 1.3)

        return max(0.0, min(1.0, score))

    def _compute_authority(self, url: str) -> float:
        """Retorna score de autoridade do domínio (0-1)."""
        from urllib.parse import urlparse

        domain = urlparse(url).netloc.lower()

        if domain.startswith("www."):
            domain = domain[4:]

        for auth_domain, score in DOMAIN_AUTHORITY.items():
            if auth_domain in domain or domain.endswith(auth_domain):
                return score

        return DOMAIN_AUTHORITY["default"]

    def _get_source_quality(self, source: str) -> float:
        """Retorna qualidade base da fonte (0-1)."""
        quality_map = {
            "github": 0.95,
            "arxiv": 0.92,
            "stackoverflow": 0.90,
            "hackernews": 0.85,
            "semantic_scholar": 0.90,
            "pubmed": 0.88,
            "reddit": 0.75,
            "producthunt": 0.80,
            "youtube": 0.70,
            "rss": 0.75,
            "web": 0.60,
            "searxng": 0.65,
            "default": 0.50,
        }
        return quality_map.get(source, quality_map["default"])

    # ── Score Fusion ──────────────────────────────────────────────────────────

    def _compute_hybrid_scores(self, candidates: List[Candidate]) -> None:
        """Combina BM25 + embeddings + heurísticas em score híbrido."""
        cfg = self.config

        for candidate in candidates:
            b = candidate.breakdown
            candidate.hybrid_score = (
                b.bm25_score * cfg.bm25_weight
                + b.embedding_score * cfg.embedding_weight
                + b.heuristic_score * cfg.heuristic_weight
            )
            b.final_score = candidate.hybrid_score

    def _compute_final_scores(self, top_k: List[Candidate]) -> None:
        """Combina hybrid score com LLM score para top-K candidatos."""
        cfg = self.config

        for candidate in top_k:
            b = candidate.breakdown
            if b.llm_score is not None:
                b.final_score = (
                    candidate.hybrid_score * (1 - cfg.llm_weight)
                    + b.llm_score * cfg.llm_weight
                )
            else:
                b.final_score = candidate.hybrid_score

    # ── LLM Re-ranking ──────────────────────────────────────────────────────

    async def _score_llm(self, top_k: List[Candidate], query: str) -> None:
        """Usa LLM para re-rankar apenas os top-K candidatos."""
        if not self.llm or not top_k:
            return

        batch_size = 10
        for i in range(0, len(top_k), batch_size):
            batch = top_k[i : i + batch_size]
            try:
                await self._llm_score_batch(batch, query, batch_offset=i)
            except Exception as e:
                logger.warning(f"LLM re-ranking falhou no batch {i}: {e}")
                for c in batch:
                    c.breakdown.llm_score = None

    async def _llm_score_batch(
        self,
        batch: List[Candidate],
        query: str,
        batch_offset: int = 0,
    ) -> None:
        """Scorea um batch de candidatos via LLM."""
        prompt = self._build_llm_prompt(batch, query, batch_offset)

        schema = {
            "type": "object",
            "properties": {
                "scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "score": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["index", "score"],
                    },
                }
            },
            "required": ["scores"],
        }

        try:
            result = await self.llm.generate_structured(prompt, schema)
            scores_data = result.get("scores", [])

            for item in scores_data:
                idx = item.get("index", -1)
                score = item.get("score", 50) / 100.0
                if 0 <= idx < len(batch):
                    batch[idx].breakdown.llm_score = max(0.0, min(1.0, score))

        except Exception as e:
            logger.warning(f"LLM structured scoring falhou: {e}")
            for c in batch:
                c.breakdown.llm_score = c.breakdown.heuristic_score

    def _build_llm_prompt(self, batch: List[Candidate], query: str, offset: int) -> str:
        """Constrói prompt para LLM re-ranking."""
        lines = [
            "Você é um avaliador de relevância de resultados de busca.",
            "Avalie cada resultado abaixo para a query do usuário.",
            "",
            f"Query: {query}",
            "",
            "Resultados (avalie de 0 a 100):",
            "",
        ]
        for i, candidate in enumerate(batch):
            result = candidate.result
            lines.append(f"[{i}] {result.title or 'Sem título'}")
            lines.append(f"    URL: {result.url}")
            lines.append(f"    Source: {result.source}")
            desc = (result.description or "")[:300]
            lines.append(f"    Descrição: {desc}")
            lines.append("")

        lines.extend(
            [
                "Responda em JSON com array de scores:",
                '{"scores": [{"index": 0, "score": 85, "reason": "muito relevante"}, ...]}',
                "Critérios: relevância para a query, qualidade da fonte, atualidade, profundidade.",
            ]
        )

        return "\n".join(lines)

    # ── Output ────────────────────────────────────────────────────────────────

    def _to_ranked_results(self, candidates: List[Candidate]) -> List[RankedResult]:
        """Converte candidatos em RankedResult com score_breakdown."""
        ranked: List[RankedResult] = []
        for i, candidate in enumerate(candidates):
            result = candidate.result
            b = candidate.breakdown
            ranked.append(
                RankedResult(
                    source=result.source,
                    title=result.title,
                    url=result.url,
                    description=result.description,
                    metrics=result.metrics,
                    raw=result.raw,
                    fetched_at=result.fetched_at,
                    confidence_score=getattr(result, "confidence_score", 0.0),
                    score=b.final_score,
                    score_breakdown={
                        "bm25": round(b.bm25_score, 4),
                        "embedding": round(b.embedding_score, 4),
                        "heuristic": round(b.heuristic_score, 4),
                        "llm": round(b.llm_score, 4)
                        if b.llm_score is not None
                        else None,
                        "freshness": round(b.freshness_score, 4),
                        "authority": round(b.authority_score, 4),
                        "source_boost": round(b.source_boost, 4),
                        "hybrid": round(candidate.hybrid_score, 4),
                        "pre_filter": round(candidate.pre_filter_score, 4),
                        "rank": i + 1,
                    },
                )
            )
        return ranked

    # ── Utilitários ─────────────────────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenização simples (minúsculas, remove pontuação)."""
        import re

        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        tokens = [t for t in text.split() if len(t) > 1]
        return tokens

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Calcula similaridade cosseno entre dois vetores."""
        if len(a) != len(b) or len(a) == 0:
            return 0.0

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    @staticmethod
    def _is_suspicious_url(url: str) -> bool:
        """Detecta URLs potencialmente spam/maliciosos."""
        suspicious_patterns = [
            "bit.ly",
            "tinyurl",
            "t.co/",
            "short.link",
            "click",
            "track",
            "affiliate",
            "ad.doubleclick",
            ".tk",
            ".ml",
            ".cf",
        ]
        url_lower = url.lower()
        return any(pat in url_lower for pat in suspicious_patterns)

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna métricas do ranker para observabilidade."""
        return {
            "config": {
                "bm25_weight": self.config.bm25_weight,
                "embedding_weight": self.config.embedding_weight,
                "heuristic_weight": self.config.heuristic_weight,
                "llm_weight": self.config.llm_weight,
                "llm_top_k": self.config.llm_top_k,
            },
            "embedding_model_loaded": self._embedding_model is not None,
            "llm_available": self.llm is not None,
        }


# ─── Stubs de Compatibilidade ───────────────────────────────────────────────


class BM25:
    """Implementação real do BM25 para compatibilidade e uso lexical."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.corpus = corpus
        self.k1 = k1
        self.b = b
        self.corpus_size = len(corpus)
        self.avg_doc_len = sum(len(doc) for doc in corpus) / max(1, self.corpus_size)

        # Document frequencies for terms
        from collections import Counter

        self.doc_freqs = Counter()
        for doc in corpus:
            for term in set(doc):
                self.doc_freqs[term] += 1

        # Inverse document frequencies
        self.idf = {}
        for term, df in self.doc_freqs.items():
            self.idf[term] = math.log((self.corpus_size - df + 0.5) / (df + 0.5) + 1.0)

    def scores_for_query(self, query: list[str]) -> list[float]:
        scores = []
        from collections import Counter

        for doc in self.corpus:
            score = 0.0
            doc_len = len(doc)
            tf = Counter(doc)
            for term in query:
                if term in tf:
                    term_tf = tf[term]
                    idf_val = self.idf.get(term, 0.0)
                    numerator = term_tf * (self.k1 + 1)
                    denominator = term_tf + self.k1 * (
                        1 - self.b + self.b * (doc_len / max(1, self.avg_doc_len))
                    )
                    score += idf_val * (numerator / denominator)
            scores.append(score)
        return scores


class SemanticScorer:
    """Classe dummy para compatibilidade de importação."""

    pass


class HybridRankResultCompat:
    """Objeto retornado pelo modo de compatibilidade do HybridRanker."""

    def __init__(
        self,
        result: SearchResult,
        final_score: float,
        heuristic_score: float,
        bm25_score: float,
        embedding_score: Optional[float],
    ):
        self.result = result
        self.final_score = final_score
        self.heuristic_score = heuristic_score
        self.bm25_score = bm25_score
        self.embedding_score = embedding_score


class HybridRankResult(HybridRankResultCompat):
    """Classe dummy/alias para compatibilidade de importação."""

    pass


def _tokenize(text: str) -> list[str]:
    """Tokenização simples (minúsculas, remove pontuação)."""
    import re

    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [t for t in text.split() if len(t) > 1]


def _normalize(scores: list[float]) -> list[float]:
    """Normaliza uma lista de scores para a escala [0, 1]."""
    if not scores:
        return []
    min_val = min(scores)
    max_val = max(scores)
    diff = max_val - min_val
    if diff == 0.0:
        return [0.0] * len(scores)
    return [(s - min_val) / diff for s in scores]


DEFAULT_PRE_FILTER_TOP_N: int = 50
DEFAULT_WEIGHTS: dict[str, float] = {
    "bm25": DEFAULT_BM25_WEIGHT,
    "embedding": DEFAULT_EMBEDDING_WEIGHT,
    "heuristic": DEFAULT_HEURISTIC_WEIGHT,
    "llm": DEFAULT_LLM_WEIGHT,
}
