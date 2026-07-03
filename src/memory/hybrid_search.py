from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class HybridSearcher:
    def __init__(self, chroma_client: Any | None = None, cohere_api_key: str | None = None):
        """
        chroma_client: uma Colecao ou Cliente do ChromaDB.
        cohere_api_key: chave de API do Cohere para reranking.
        """
        self.chroma = chroma_client
        self.cohere_key = cohere_api_key
        self._documents: list[dict[str, Any]] = []  # Cache local para BM25
        self._encoder: Any | None = None

    def _get_encoder(self) -> Any | None:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._encoder = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("HybridSearcher: Modelo sentence-transformers carregado.")
            except Exception as e:
                logger.warning(f"Nao foi possivel carregar o modelo sentence-transformers: {e}")
        return self._encoder

    def index_documents(self, documents: list[dict[str, Any]]) -> None:
        """Cache em memoria para consultas BM25. Cada documento deve ser um dict com no minimo a chave 'text'."""
        self._documents = documents

    async def search(self, query: str, top_k: int = 15) -> list[dict[str, Any]]:
        if not query:
            return []

        # 1. Busca lexical via BM25
        bm25_results = self._bm25_search(query, top_k * 2)

        # 2. Busca vetorial via ChromaDB
        chroma_results = await self._chroma_search(query, top_k * 2)

        # 3. Reciprocal Rank Fusion (RRF)
        combined = self._reciprocal_rank_fusion(bm25_results, chroma_results)

        # 4. Rerank com Cohere se disponivel
        if self.cohere_key and combined:
            reranked = await self._cohere_rerank(query, combined[:top_k * 2], top_k)
            return reranked

        return combined[:top_k]

    def _bm25_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        if not self._documents:
            return []
        try:
            from rank_bm25 import BM25Okapi
            corpus = [d.get("text", "").split() for d in self._documents]
            bm25 = BM25Okapi(corpus)
            scores = bm25.get_scores(query.split())
            paired = sorted(zip(self._documents, scores), key=lambda x: x[1], reverse=True)
            return [doc for doc, score in paired[:top_k] if score > 0]
        except ImportError:
            logger.warning("rank_bm25 nao instalado. Ignorando busca BM25.")
            return []
        except Exception as e:
            logger.warning(f"Erro na busca BM25: {e}")
            return []

    async def _chroma_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        if not self.chroma:
            return []
        encoder = self._get_encoder()
        if encoder is None:
            return []
        try:
            embedding = encoder.encode(query).tolist()
            # Suporta se for Collection
            if hasattr(self.chroma, "query"):
                results = self.chroma.query(query_embeddings=[embedding], n_results=top_k)
            # Se for Client (como fallback), tenta obter a colecao "sra_memories"
            elif hasattr(self.chroma, "get_collection"):
                col = self.chroma.get_collection("sra_memories")
                results = col.query(query_embeddings=[embedding], n_results=top_k)
            else:
                return []

            if not results or not results.get("documents") or not results["documents"][0]:
                return []

            docs = []
            for doc, id_ in zip(results["documents"][0], results["ids"][0]):
                docs.append({"text": doc, "id": id_})
            return docs
        except Exception as e:
            logger.warning(f"Busca ChromaDB falhou: {e}")
            return []

    def _reciprocal_rank_fusion(self, *result_lists: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        doc_map: dict[str, dict[str, Any]] = {}

        for results in result_lists:
            for rank, doc in enumerate(results):
                doc_id = doc.get("url") or doc.get("id") or doc.get("text", "")[:100]
                if not doc_id:
                    continue
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
                # Faz merge de metadados se o documento ja existir
                if doc_id in doc_map:
                    doc_map[doc_id].update(doc)
                else:
                    doc_map[doc_id] = doc.copy()

        sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [doc_map[key] for key in sorted_keys]

    async def _cohere_rerank(self, query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        try:
            import cohere
            co = cohere.AsyncClient(api_key=self.cohere_key)
            texts = [d.get("text", "")[:2000] for d in docs]
            response = await co.rerank(
                query=query,
                documents=texts,
                top_n=top_k,
                model="rerank-v3.5"
            )
            return [docs[r.index] for r in response.results]
        except Exception as e:
            logger.warning(f"Cohere Rerank falhou: {e}")
            return docs[:top_k]