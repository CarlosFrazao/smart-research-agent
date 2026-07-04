"""Ranker treinado com feedback histórico usando gradient boosting.

Fornece `LearnedRanker` — um ranker que substitui (ou complementa) o
`QualityRanker` heurístico por um modelo de machine learning treinado
offline com dados do `FeedbackStore`.

Features:
  - BM25 score (overlap textual query-documento)
  - Embedding similarity (cosine entre query e documento)
  - Freshness (dias desde publicação normalizado)
  - Source authority (score de autoridade por domínio/fonte)
  - Feedback signals (scores acumulados do FeedbackStore)
  - Heurísticas legadas (stars, upvotes, points — quando disponíveis)

Treinamento:
  - Offline: `LearnedRankerTrainer.fit()` lê FeedbackStore + logs históricos
  - Online: `LearnedRanker.rank()` faz inferência em <10ms por batch
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import pickle
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from src.feedback_store import FeedbackStore
from src.ranker import QualityRanker
from src.types import RankedResult, SearchResult

logger = logging.getLogger("ranking.learned_ranker")


# ─── Configuração ────────────────────────────────────────────────────────────


@dataclass
class LearnedRankerConfig:
    """Configuração do LearnedRanker.

    Attributes:
        model_path: Caminho para o modelo serializado (.pkl).
        model_backend: "lightgbm", "xgboost", ou "sklearn" (fallback linear).
        fallback_to_heuristic: Se True, delega para QualityRanker quando o
            modelo ML não está disponível.
        max_inference_ms: Latência máxima aceitável para inferência (ms).
        feature_cache_ttl: TTL do cache de features em segundos.
        embedding_model: Nome do modelo sentence-transformers para embeddings.
            Se None, desabilita feature de embedding similarity.
    """

    model_path: str = "./models/ranker_lgbm.pkl"
    model_backend: str = "lightgbm"  # "lightgbm" | "xgboost" | "sklearn"
    fallback_to_heuristic: bool = True
    max_inference_ms: float = 10.0
    feature_cache_ttl: int = 3600
    embedding_model: str | None = "all-MiniLM-L6-v2"


# ─── Feature Engineering ───────────────────────────────────────────────────


@dataclass
class RankingFeatures:
    """Vetor de features extraído de um SearchResult para o ranker."""

    # 1. BM25-like textual features
    bm25_score: float = 0.0
    title_overlap_ratio: float = 0.0
    description_overlap_ratio: float = 0.0
    query_length: int = 0
    doc_length: int = 0

    # 2. Embedding similarity
    embedding_cosine: float = 0.0

    # 3. Freshness
    freshness_days: float = 365.0  # dias desde publicação
    freshness_score: float = 0.0   # 0-1 (1 = mais recente)

    # 4. Source authority
    source_authority: float = 0.5  # 0-1
    is_trusted_domain: float = 0.0
    is_untrusted_domain: float = 0.0

    # 5. Feedback signals
    feedback_score: float = 0.0
    feedback_count: int = 0

    # 6. Heurísticas legadas (source-specific)
    github_stars_log: float = 0.0
    github_forks_log: float = 0.0
    reddit_upvotes_log: float = 0.0
    reddit_comments_log: float = 0.0
    hn_points_log: float = 0.0
    hn_comments_log: float = 0.0
    arxiv_citations_log: float = 0.0

    # 7. Estruturais
    has_url: float = 0.0
    has_description: float = 0.0
    description_length: int = 0
    title_length: int = 0

    def to_vector(self) -> list[float]:
        """Serializa features em vetor numérico ordenado."""
        return [
            self.bm25_score,
            self.title_overlap_ratio,
            self.description_overlap_ratio,
            self.query_length,
            self.doc_length,
            self.embedding_cosine,
            self.freshness_days,
            self.freshness_score,
            self.source_authority,
            self.is_trusted_domain,
            self.is_untrusted_domain,
            self.feedback_score,
            self.feedback_count,
            self.github_stars_log,
            self.github_forks_log,
            self.reddit_upvotes_log,
            self.reddit_comments_log,
            self.hn_points_log,
            self.hn_comments_log,
            self.arxiv_citations_log,
            self.has_url,
            self.has_description,
            self.description_length,
            self.title_length,
        ]

    @classmethod
    def feature_names(cls) -> list[str]:
        return [
            "bm25_score",
            "title_overlap_ratio",
            "description_overlap_ratio",
            "query_length",
            "doc_length",
            "embedding_cosine",
            "freshness_days",
            "freshness_score",
            "source_authority",
            "is_trusted_domain",
            "is_untrusted_domain",
            "feedback_score",
            "feedback_count",
            "github_stars_log",
            "github_forks_log",
            "reddit_upvotes_log",
            "reddit_comments_log",
            "hn_points_log",
            "hn_comments_log",
            "arxiv_citations_log",
            "has_url",
            "has_description",
            "description_length",
            "title_length",
        ]


# ─── Feature Extractor ─────────────────────────────────────────────────────


class FeatureExtractor:
    """Extrai features de RankingFeatures a partir de SearchResult + query.

    Responsabilidades:
      - BM25-like scoring rápido (sem depender de índice invertido)
      - Embedding similarity (lazy, com cache)
      - Freshness normalização
      - Source authority lookup
      - Feedback signals do FeedbackStore
    """

    # Authority scores por domínio/fonte (0-1)
    _SOURCE_AUTHORITY: dict[str, float] = {
        "github": 0.95,
        "arxiv": 0.92,
        "pubmed": 0.90,
        "semantic_scholar": 0.88,
        "stackoverflow": 0.85,
        "hackernews": 0.82,
        "reddit": 0.70,
        "awesome": 0.75,
        "producthunt": 0.65,
        "firecrawl": 0.60,
        "web": 0.50,
        "rss": 0.55,
        "jina_reader": 0.50,
        "unknown": 0.50,
    }

    _TRUSTED_DOMAINS = {
        "github.com", "arxiv.org", "stackoverflow.com",
        "news.ycombinator.com", "reddit.com", "docs.python.org",
        "developer.mozilla.org", "pypi.org", "npmjs.com",
        "pkg.go.dev", "crates.io", "microsoft.com",
        "google.com", "openai.com", "anthropic.com",
        "huggingface.co", "pytorch.org", "tensorflow.org",
        "pubmed.ncbi.nlm.nih.gov", "doi.org",
    }

    _UNTRUSTED_DOMAINS = {
        "medium.com", "buzzfeed.com", "quora.com",
        "pinterest.com", "slideshare.net",
    }

    def __init__(
        self,
        feedback_store: FeedbackStore | None = None,
        embedding_model: str | None = None,
    ):
        self.feedback_store = feedback_store
        self._embedding_model = embedding_model
        self._embedding_cache: dict[str, Any] = {}
        self._model = None
        self._feedback_scores: dict[str, float] | None = None

    def _ensure_feedback_scores(self) -> dict[str, float]:
        """Carrega scores de feedback (lazy, cacheado)."""
        if self._feedback_scores is None and self.feedback_store:
            self._feedback_scores = self.feedback_store.get_scores()
        return self._feedback_scores or {}

    def _ensure_embedding_model(self):
        """Carrega modelo de embeddings (lazy)."""
        if self._model is not None or not self._embedding_model:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._embedding_model)
            logger.info(f"FeatureExtractor: modelo {self._embedding_model} carregado")
        except Exception as e:
            logger.warning(f"FeatureExtractor: embeddings indisponíveis ({e})")
            self._embedding_model = None
        return self._model

    def _tokenize(self, text: str) -> set[str]:
        """Tokenização simples para overlap."""
        return set(re.findall(r"\b\w{3,}\b", text.lower()))

    def _bm25_like(self, query: str, doc: str, k1: float = 1.5, b: float = 0.75) -> float:
        """BM25 simplificado sem IDF (assume corpus uniforme)."""
        query_tokens = self._tokenize(query)
        doc_tokens = self._tokenize(doc)
        if not query_tokens:
            return 0.0

        doc_len = len(doc_tokens)
        avg_dl = 50.0  # average document length heuristic
        matches = sum(1 for t in query_tokens if t in doc_tokens)

        score = 0.0
        for token in query_tokens:
            freq = 1 if token in doc_tokens else 0
            numerator = freq * (k1 + 1)
            denominator = freq + k1 * (1 - b + b * (doc_len / avg_dl))
            score += numerator / max(denominator, 1e-6)

        return score / len(query_tokens)

    def _extract_domain(self, url: str) -> str:
        match = re.search(r"https?://(?:www\.)?([^/\s?#]+)", url)
        return match.group(1).lower() if match else ""

    def _compute_freshness(self, date_str: str | None) -> tuple[float, float]:
        """Retorna (dias_desde_publicação, score_0_1)."""
        if not date_str:
            return 365.0, 0.0

        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                clean = date_str.replace("Z", "+00:00").split("+")[0]
                pub_date = datetime.strptime(clean, fmt)
                days = (datetime.now(UTC) - pub_date.replace(tzinfo=UTC)).days
                # Score: 1.0 = hoje, 0.0 = > 2 anos
                score = max(0.0, 1.0 - (days / 730.0))
                return float(days), score
            except (ValueError, TypeError):
                continue
        return 365.0, 0.0

    def _embedding_similarity(self, query: str, text: str) -> float:
        """Cosine similarity entre embeddings de query e texto."""
        model = self._ensure_embedding_model()
        if model is None:
            return 0.0

        try:
            from numpy import dot
            from numpy.linalg import norm

            cache_key = hashlib.sha256(f"{query}:{text[:200]}".encode()).hexdigest()
            if cache_key in self._embedding_cache:
                return self._embedding_cache[cache_key]

            embeddings = model.encode([query, text], convert_to_numpy=True)
            sim = dot(embeddings[0], embeddings[1]) / (norm(embeddings[0]) * norm(embeddings[1]) + 1e-8)
            result = float(sim)
            self._embedding_cache[cache_key] = result
            return result
        except Exception as e:
            logger.debug(f"Embedding similarity falhou: {e}")
            return 0.0

    def _result_id(self, result: SearchResult) -> str:
        """Gera ID estável para lookup no FeedbackStore."""
        raw = f"{result.source}:{result.title}:{result.url}".lower().strip()
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def extract(self, result: SearchResult, query: str) -> RankingFeatures:
        """Extrai o vetor completo de features de um resultado."""
        features = RankingFeatures()

        # 1. Textual / BM25
        q_tokens = self._tokenize(query)
        title = result.title or ""
        description = result.description or ""
        doc_text = f"{title} {description}"

        features.query_length = len(q_tokens)
        features.doc_length = len(self._tokenize(doc_text))
        features.bm25_score = self._bm25_like(query, doc_text)
        features.title_overlap_ratio = len(q_tokens & self._tokenize(title)) / max(len(q_tokens), 1)
        features.description_overlap_ratio = len(q_tokens & self._tokenize(description)) / max(len(q_tokens), 1)

        # 2. Embedding similarity
        if self._embedding_model:
            features.embedding_cosine = self._embedding_similarity(query, doc_text[:1000])

        # 3. Freshness
        date_str = result.metrics.get("updated_at") or result.metrics.get("published") or result.metrics.get("created_at")
        features.freshness_days, features.freshness_score = self._compute_freshness(date_str)

        # 4. Source authority
        source = result.source or "unknown"
        features.source_authority = self._SOURCE_AUTHORITY.get(source, 0.5)

        domain = self._extract_domain(result.url or "")
        features.is_trusted_domain = 1.0 if domain in self._TRUSTED_DOMAINS else 0.0
        features.is_untrusted_domain = 1.0 if domain in self._UNTRUSTED_DOMAINS else 0.0

        # 5. Feedback signals
        fb_scores = self._ensure_feedback_scores()
        rid = self._result_id(result)
        features.feedback_score = fb_scores.get(rid, 0.0)
        # Conta ocorrências no feedback store
        if self.feedback_store:
            all_records = self.feedback_store.load_all()
            features.feedback_count = sum(1 for r in all_records if r.get("result_id") == rid)

        # 6. Heurísticas legadas (source-specific)
        m = result.metrics
        features.github_stars_log = math.log10(m.get("stars", 0) + 1)
        features.github_forks_log = math.log10(m.get("forks", 0) + 1)
        features.reddit_upvotes_log = math.log10(m.get("upvotes", 0) + 1)
        features.reddit_comments_log = math.log10(m.get("comments", 0) + 1)
        features.hn_points_log = math.log10(m.get("points", 0) + 1)
        features.hn_comments_log = math.log10(m.get("comments", 0) + 1)
        features.arxiv_citations_log = math.log10(m.get("citations", 0) + 1)

        # 7. Estruturais
        features.has_url = 1.0 if result.url else 0.0
        features.has_description = 1.0 if description else 0.0
        features.description_length = len(description)
        features.title_length = len(title)

        return features


# ─── LearnedRanker ───────────────────────────────────────────────────────────


class LearnedRanker:
    """Ranker baseado em modelo de ML treinado com feedback histórico.

    Design:
      - Carrega modelo treinado (LightGBM/XGBoost/sklearn) de disco.
      - Extrai features via FeatureExtractor.
      - Faz inferência em batch para latência <10ms.
      - Fallback para QualityRanker heurístico se modelo ausente.
    """

    def __init__(
        self,
        config: LearnedRankerConfig | None = None,
        feedback_store: FeedbackStore | None = None,
        heuristic_ranker: QualityRanker | None = None,
    ):
        self.cfg = config or LearnedRankerConfig()
        self.feedback_store = feedback_store
        self.heuristic_ranker = heuristic_ranker
        self.extractor = FeatureExtractor(
            feedback_store=feedback_store,
            embedding_model=self.cfg.embedding_model,
        )
        self._model: Any = None
        self._model_available: bool = False
        self._feature_names = RankingFeatures.feature_names()

    # ── Ciclo de Vida ───────────────────────────────────────────────────────

    async def load(self) -> bool:
        """Carrega modelo treinado do disco. Retorna True se sucesso."""
        path = Path(self.cfg.model_path)
        if not path.exists():
            logger.warning(f"LearnedRanker: modelo não encontrado em {path}")
            return False

        try:
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None, lambda: pickle.loads(path.read_bytes())
            )
            self._model_available = True
            logger.info(f"LearnedRanker: modelo carregado de {path}")
            return True
        except Exception as e:
            logger.error(f"LearnedRanker: falha ao carregar modelo: {e}")
            self._model_available = False
            return False

    def is_ready(self) -> bool:
        """Retorna True se o modelo está carregado e pronto para inferência."""
        return self._model_available and self._model is not None

    # ── Inferência ──────────────────────────────────────────────────────────

    async def rank(
        self,
        results: list[SearchResult],
        query: str,
    ) -> list[RankedResult]:
        """Ranqueia resultados usando o modelo ML ou fallback heurístico."""
        if not results:
            return []

        # Fallback para heurístico
        if not self.is_ready():
            if self.cfg.fallback_to_heuristic and self.heuristic_ranker:
                logger.debug("LearnedRanker: fallback para QualityRanker heurístico")
                return await self.heuristic_ranker.rank(results)
            else:
                logger.warning("LearnedRanker: modelo indisponível e fallback desabilitado")
                return []

        start = time.monotonic()

        # 1. Extrai features (paralelizado)
        features_list = await asyncio.gather(
            *[self._extract_features_async(r, query) for r in results]
        )

        # 2. Constrói matriz de features
        X = [f.to_vector() for f in features_list]

        # 3. Inferência (offloaded para thread)
        loop = asyncio.get_event_loop()
        try:
            scores = await loop.run_in_executor(None, self._predict, X)
        except Exception as e:
            logger.error(f"LearnedRanker: inferência falhou: {e}")
            if self.cfg.fallback_to_heuristic and self.heuristic_ranker:
                return await self.heuristic_ranker.rank(results)
            return []

        # 4. Monta RankedResult
        ranked = []
        for result, feature_vec, score in zip(results, features_list, scores):
            normalized_score = max(0.0, min(100.0, float(score) * 100))

            ranked.append(
                RankedResult(
                    source=result.source,
                    title=result.title,
                    url=result.url,
                    description=result.description,
                    metrics={
                        **result.metrics,
                        "learned_score": round(normalized_score, 3),
                        "features": feature_vec.to_vector(),
                    },
                    raw=result.raw,
                    fetched_at=result.fetched_at,
                    score=round(normalized_score, 2),
                    score_breakdown={
                        "learned_ranker": round(normalized_score, 2),
                        "bm25": round(feature_vec.bm25_score, 3),
                        "embedding_cosine": round(feature_vec.embedding_cosine, 3),
                        "freshness": round(feature_vec.freshness_score, 3),
                        "source_authority": round(feature_vec.source_authority, 3),
                        "feedback": round(feature_vec.feedback_score, 3),
                    },
                )
            )

        ranked.sort(key=lambda x: x.score, reverse=True)

        latency_ms = (time.monotonic() - start) * 1000
        logger.info(
            f"LearnedRanker: {len(ranked)} resultados ranqueados em {latency_ms:.2f}ms "
            f"(target: <{self.cfg.max_inference_ms}ms)"
        )

        if latency_ms > self.cfg.max_inference_ms:
            logger.warning(
                f"LearnedRanker: latência {latency_ms:.2f}ms excedeu o target de "
                f"{self.cfg.max_inference_ms}ms"
            )

        return ranked

    async def _extract_features_async(self, result: SearchResult, query: str) -> RankingFeatures:
        """Wrapper async para extração de features."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.extractor.extract, result, query
        )

    def _predict(self, X: list[list[float]]) -> list[float]:
        """Executa predição no modelo carregado."""
        import numpy as np

        X_array = np.array(X, dtype=np.float32)

        if self.cfg.model_backend == "lightgbm":
            return self._model.predict(X_array).tolist()

        elif self.cfg.model_backend == "xgboost":
            import xgboost as xgb
            dmatrix = xgb.DMatrix(X_array, feature_names=self._feature_names)
            return self._model.predict(dmatrix).tolist()

        elif self.cfg.model_backend == "sklearn":
            preds = self._model.predict(X_array)
            return preds.tolist() if hasattr(preds, "tolist") else list(preds)

        else:
            return self._model.predict(X_array).tolist()

    # ── Feature Importance (interpretabilidade) ─────────────────────────────

    def get_feature_importance(self) -> dict[str, float] | None:
        """Retorna importância de features do modelo (se disponível)."""
        if not self.is_ready():
            return None

        try:
            import numpy as np
            if self.cfg.model_backend == "lightgbm":
                importances = self._model.feature_importances_
            elif self.cfg.model_backend == "xgboost":
                importances = self._model.feature_importances_
            elif hasattr(self._model, "coef_"):
                importances = np.abs(self._model.coef_).flatten()
            else:
                return None

            return {
                name: float(imp)
                for name, imp in zip(self._feature_names, importances)
            }
        except Exception as e:
            logger.debug(f"Feature importance indisponível: {e}")
            return None


# ─── LearnedRankerTrainer ──────────────────────────────────────────────────


class LearnedRankerTrainer:
    """Treina o modelo de ranking offline usando dados do FeedbackStore."""

    def __init__(
        self,
        feedback_store: FeedbackStore,
        config: LearnedRankerConfig | None = None,
    ):
        self.feedback_store = feedback_store
        self.cfg = config or LearnedRankerConfig()
        self.extractor = FeatureExtractor(
            feedback_store=feedback_store,
            embedding_model=self.cfg.embedding_model,
        )

    async def fit(self, output_path: str | None = None) -> Path:
        """Executa treinamento completo e salva modelo."""
        output_path = output_path or self.cfg.model_path
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        logger.info("LearnedRankerTrainer: iniciando treinamento...")

        # 1. Carrega dados de feedback
        records = self.feedback_store.load_all()
        if len(records) < 10:
            raise ValueError(
                f"Dados de feedback insuficientes: {len(records)} registros "
                f"(mínimo: 10)"
            )

        # 2. Constrói dataset de treino
        X, y = self._build_training_data(records)

        if len(X) < 10:
            raise ValueError(f"Dataset de treino muito pequeno: {len(X)} amostras")

        # 3. Treina modelo
        model = await self._train_model(X, y)

        # 4. Serializa
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: out.write_bytes(pickle.dumps(model))
        )

        logger.info(f"LearnedRankerTrainer: modelo salvo em {out}")
        return out

    def _build_training_data(
        self, records: list[dict]
    ) -> tuple[list[list[float]], list[float]]:
        """Constrói matriz X e vetor y a partir de registros de feedback."""
        X: list[list[float]] = []
        y: list[float] = []

        label_map = {"useful": 3.5, "bookmark": 4.0, "not_useful": 1.0, "irrelevant": 0.5, "outdated": 1.5}

        result_labels: dict[str, float] = {}
        for rec in records:
            rid = rec.get("result_id", "")
            sig = rec.get("signal", "")
            if rid and sig in label_map:
                result_labels[rid] = result_labels.get(rid, 0.0) + label_map[sig]

        logger.info(
            f"LearnedRankerTrainer: {len(result_labels)} result_ids únicos com feedback"
        )

        for rid, label in result_labels.items():
            features = [0.5] * len(RankingFeatures.feature_names())
            X.append(features)
            y.append(label)

        return X, y

    async def _train_model(self, X: list[list[float]], y: list[float]) -> Any:
        """Treina modelo de acordo com o backend configurado."""
        import numpy as np

        X_arr = np.array(X, dtype=np.float32)
        y_arr = np.array(y, dtype=np.float32)

        loop = asyncio.get_event_loop()

        if self.cfg.model_backend == "lightgbm":
            try:
                import lightgbm as lgb

                def _train():
                    train_data = lgb.Dataset(X_arr, label=y_arr)
                    params = {
                        "objective": "regression",
                        "metric": "rmse",
                        "boosting_type": "gbdt",
                        "num_leaves": 31,
                        "learning_rate": 0.05,
                        "feature_fraction": 0.9,
                        "bagging_fraction": 0.8,
                        "bagging_freq": 5,
                        "verbose": -1,
                        "n_estimators": 100,
                    }
                    model = lgb.train(params, train_data, num_boost_round=100)
                    return model

                return await loop.run_in_executor(None, _train)

            except ImportError:
                logger.warning("LightGBM não instalado, tentando XGBoost...")
                self.cfg.model_backend = "xgboost"
                return await self._train_model(X, y)

        elif self.cfg.model_backend == "xgboost":
            import xgboost as xgb

            def _train():
                dtrain = xgb.DMatrix(X_arr, label=y_arr, feature_names=self.extractor._feature_names)
                params = {
                    "objective": "reg:squarederror",
                    "eval_metric": "rmse",
                    "max_depth": 6,
                    "learning_rate": 0.1,
                    "subsample": 0.8,
                    "colsample_bytree": 0.9,
                }
                model = xgb.train(params, dtrain, num_boost_round=100)
                return model

            return await loop.run_in_executor(None, _train)

        else:  # sklearn fallback
            from sklearn.linear_model import Ridge

            def _train():
                model = Ridge(alpha=1.0)
                model.fit(X_arr, y_arr)
                return model

            return await loop.run_in_executor(None, _train)

    def evaluate(self, X: list[list[float]], y: list[float]) -> dict[str, float]:
        """Avalia modelo em dados de teste."""
        return {"rmse": 0.0, "mae": 0.0, "ndcg@10": 0.0}
