"""Reranqueador semantico de resultados usando embeddings e modelos de similaridade."""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)
SearchResult = dict


def _tokenize(text):
    """Tokeniza texto em um conjunto de palavras de 3+ caracteres (lowercase)."""
    return {w.lower() for w in re.findall(r"\b\w{3,}\b", text)}


def _keyword_score(query, result):
    """Calcula score de overlap de keywords entre a query e o resultado."""
    if not query:
        return 0.0
    q = _tokenize(query)
    if not q:
        return 0.0
    content = " ".join(
        [
            result.get("title", ""),
            result.get("snippet", ""),
            result.get("content", ""),
            result.get("url", ""),
        ]
    )
    r = _tokenize(content)
    return len(q & r) / len(q)


class SemanticReranker:
    """Re-ranqueia resultados usando embeddings semanticos com fallback para keyword-overlap.

    Usa sentence-transformers (modelo MiniLM) de forma lazy (carregamento na primeira chamada).
    Se o modelo nao estiver disponivel, usa keyword-reranking como fallback.
    """

    _MODEL_NAME = "all-MiniLM-L6-v2"

    # C1/F4 — teto de tempo para o download do peso HF (segundos). Em ambiente
    # sem rede o download pode travar a pipeline; cortamos curto e caímos no
    # reranking por score local (sem crash).
    _MODEL_LOAD_TIMEOUT = 30.0

    def __init__(self):
        self._model = None
        self._util = None
        self._model_available = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _hf_offline() -> bool:
        """Detecta ambiente HF offline (sem rede / sem token de download).

        Se ``HF_HUB_OFFLINE=1`` ou ``TRANSFORMERS_OFFLINE=1`` estão setados,
        OU não há ``HF_TOKEN``/``HUGGINGFACE_HUB_TOKEN`` e não há cache local
        do modelo, o download falharia — vamos direto ao fallback local sem
        tentar baixar (economiza latência + evita travamento, F4).
        """
        import os

        if os.environ.get("HF_HUB_OFFLINE") == "1":
            return True
        if os.environ.get("TRANSFORMERS_OFFLINE") == "1":
            return True
        # Sem token de HF: download anônimo pode ser rate-limited ou bloqueado
        # em ambiente sem rede; o fallback local cobre esse caso sem crash.
        if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")):
            return True
        return False

    async def _ensure_model(self):
        """Carrega o modelo de embeddings de forma lazy e thread-safe.

        C1/F4: se o ambiente é HF offline (sem token/rede) OU o download do
        peso exceder o teto de tempo, usa reranking por score local
        (_keyword_rerank) — sem crash e com log INFO (não WARNING).
        """
        if self._model_available is not None:
            return self._model_available
        async with self._lock:
            if self._model_available is not None:
                return self._model_available
            # C1/F4 — ambiente offline explícito: pula download, vai ao fallback.
            if self._hf_offline():
                self._model_available = False
                logger.info(
                    "SemanticReranker: ambiente HF offline (sem token/rede). "
                    "Usando reranking por score local (fallback offline)."
                )
                return self._model_available
            try:
                from sentence_transformers import SentenceTransformer, util

                logger.info(f"SemanticReranker: carregando modelo {self._MODEL_NAME}")
                loop = asyncio.get_event_loop()

                async def _load():
                    return await loop.run_in_executor(
                        None, lambda: SentenceTransformer(self._MODEL_NAME)
                    )

                # C1/F4 — teto de tempo no download para não travar a pipeline.
                self._model = await asyncio.wait_for(
                    _load(), timeout=self._MODEL_LOAD_TIMEOUT
                )
                self._util = util
                self._model_available = True
                logger.info("SemanticReranker: modelo carregado com sucesso")
            except Exception as e:
                self._model_available = False
                logger.info(
                    f"SemanticReranker: modelo indisponivel ({e}). "
                    "Usando reranking por score local (fallback offline)."
                )
        return self._model_available

    async def rerank(self, query, results, top_k=None):
        """Re-ranqueia resultados usando semantica ou keyword-fallback."""
        if not results or not query:
            return results
        # C1/F4 — qualquer falha ao garantir o modelo (timeout de download,
        # erro de rede, etc.) degrada para reranking por score local, sem crash.
        try:
            available = await self._ensure_model()
        except Exception as e:
            logger.info(
                f"SemanticReranker: falha ao garantir modelo ({e}). "
                "Usando reranking por score local (fallback offline)."
            )
            available = False
        reranked = (
            await self._semantic_rerank(query, results)
            if available
            else self._keyword_rerank(query, results)
        )
        return reranked[:top_k] if top_k else reranked

    async def _semantic_rerank(self, query, results):
        """Re-ranqueia resultados usando similaridade de cosseno com embeddings."""
        try:
            docs = [
                " ".join(
                    filter(
                        None,
                        [
                            r.get("title", ""),
                            r.get("snippet", ""),
                            r.get("content", "")[:512],
                        ],
                    )
                )
                for r in results
            ]
            loop = asyncio.get_event_loop()
            embs = await loop.run_in_executor(
                None,
                lambda: self._model.encode(
                    [query] + docs, convert_to_tensor=True, show_progress_bar=False
                ),
            )
            scores = self._util.cos_sim(embs[0], embs[1:])[0].tolist()
            scored = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)
            reranked = []
            for s, r in scored:
                e = dict(r)
                e["_semantic_score"] = round(float(s), 4)
                reranked.append(e)
            logger.debug(
                f"SemanticReranker: {len(reranked)} resultados reordenados (semantico)"
            )
            return reranked
        except Exception as ex:
            logger.warning(f"SemanticReranker: erro ({ex}), usando fallback")
            return self._keyword_rerank(query, results)

    def _keyword_rerank(self, query, results):
        """Re-ranqueia resultados usando keyword-overlap como fallback."""
        scored = sorted(results, key=lambda r: _keyword_score(query, r), reverse=True)
        for r in scored:
            r.setdefault("_semantic_score", None)
        logger.debug(
            f"SemanticReranker: {len(scored)} resultados reordenados (keyword-fallback)"
        )
        return scored
