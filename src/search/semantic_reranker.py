from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)
SearchResult = dict

def _tokenize(text):
    return {w.lower() for w in re.findall(r'\b\w{3,}\b', text)}

def _keyword_score(query, result):
    if not query: return 0.0
    q = _tokenize(query)
    if not q: return 0.0
    content = ' '.join([result.get('title',''), result.get('snippet',''), result.get('content',''), result.get('url','')])
    r = _tokenize(content)
    return len(q & r) / len(q)

class SemanticReranker:
    _MODEL_NAME = 'all-MiniLM-L6-v2'
    def __init__(self):
        self._model = None
        self._util = None
        self._model_available = None
        self._lock = asyncio.Lock()
    async def _ensure_model(self):
        if self._model_available is not None: return self._model_available
        async with self._lock:
            if self._model_available is not None: return self._model_available
            try:
                from sentence_transformers import SentenceTransformer, util
                logger.info(f'SemanticReranker: carregando modelo {self._MODEL_NAME}')
                loop = asyncio.get_event_loop()
                self._model = await loop.run_in_executor(None, lambda: SentenceTransformer(self._MODEL_NAME))
                self._util = util
                self._model_available = True
                logger.info('SemanticReranker: modelo carregado com sucesso')
            except Exception as e:
                self._model_available = False
                logger.warning(f'SemanticReranker: modelo indisponivel ({e}). Fallback ativo.')
        return self._model_available
    async def rerank(self, query, results, top_k=None):
        if not results or not query: return results
        available = await self._ensure_model()
        reranked = await self._semantic_rerank(query, results) if available else self._keyword_rerank(query, results)
        return reranked[:top_k] if top_k else reranked
    async def _semantic_rerank(self, query, results):
        try:
            docs = [' '.join(filter(None,[r.get('title',''),r.get('snippet',''),r.get('content','')[:512]])) for r in results]
            loop = asyncio.get_event_loop()
            embs = await loop.run_in_executor(None, lambda: self._model.encode([query]+docs, convert_to_tensor=True, show_progress_bar=False))
            scores = self._util.cos_sim(embs[0], embs[1:])[0].tolist()
            scored = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)
            reranked = []
            for s, r in scored:
                e = dict(r)
                e['_semantic_score'] = round(float(s), 4)
                reranked.append(e)
            logger.debug(f'SemanticReranker: {len(reranked)} resultados reordenados (semantico)')
            return reranked
        except Exception as ex:
            logger.warning(f'SemanticReranker: erro ({ex}), usando fallback')
            return self._keyword_rerank(query, results)
    def _keyword_rerank(self, query, results):
        scored = sorted(results, key=lambda r: _keyword_score(query, r), reverse=True)
        for r in scored: r.setdefault('_semantic_score', None)
        logger.debug(f'SemanticReranker: {len(scored)} resultados reordenados (keyword-fallback)')
        return scored