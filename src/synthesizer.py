"""Módulo de síntese e agrupamento de resultados de pesquisa ranqueados.

Agrupa resultados por entidade-chave, mescla clústeres e gera `SynthesizedResult`
com score combinado, veredicto, TL;DR e estimativa de leitura.

Pipeline: dedupe (lexico -> fuzzy) -> clusterizacao (semantica -> lexica) -> merge -> ordenacao.

Clusterizacao semantica e deduplicacao fuzzy usam dependencias opcionais
(`sentence-transformers`, `rapidfuzz`). Se ausentes, o modulo cai automaticamente
para as estrategias lexicas originais (SequenceMatcher via `Deduplicator` e
agrupamento por palavra-chave do titulo) sem alterar o contrato publico.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
from collections import defaultdict
from typing import Any

from src.clients.llm_client import LLMClient
from src.types import RankedResult, SynthesizedResult, Verdict
from src.utils.deduplicator import Deduplicator

logger = logging.getLogger(__name__)

_STOPWORDS = {"the", "a", "an", "is", "are", "best", "top", "new", "open"}

# ── Clusterizacao semantica (embeddings) ────────────────────────────────────
_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"  # mesmo modelo usado em SemanticReranker/hybrid_search
_SEMANTIC_SIMILARITY_THRESHOLD = 0.72  # cosine sim minima para unir ao mesmo cluster
_EMBED_CHUNK_SIZE = 64  # itens por lote de encode, processados em paralelo

# ── Deduplicacao fuzzy (rapidfuzz) ──────────────────────────────────────────
_FUZZY_DEDUPE_THRESHOLD = 88  # score 0-100 (token_sort_ratio) para considerar duplicata
_FUZZY_PAIR_CHUNK_SIZE = 4000  # pares por lote, processados em paralelo
_FUZZY_DEDUPE_MAX_ITEMS = 1500  # acima disso o custo O(n^2) nao compensa; pula o passo


class Synthesizer:
    """Sintetiza e agrupa resultados ranqueados em entidades consolidadas.

    Realiza deduplicacao (exata/lexica + fuzzy), agrupa por entidade semantica
    (embeddings, com fallback lexico), mescla métricas e gera veredictos
    contextuais para cada grupo.
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client
        self.deduplicator = Deduplicator()
        self._embedder: Any = None
        self._embedder_available: bool | None = None
        self._embedder_lock = asyncio.Lock()

    async def synthesize(self, results: list[RankedResult]) -> list[SynthesizedResult]:
        """Sintetiza resultados ranqueados em entidades consolidadas e ordenadas.

        Fases: deduplicacao (lexica -> fuzzy) -> clusterizacao (semantica -> lexica)
        -> mesclagem -> ordenacao.

        Args:
            results: Lista de `RankedResult` ranqueados pelo `QualityRanker`.

        Returns:
            list[SynthesizedResult]: Entidades sintetizadas, ordenadas por
                ``combined_score`` descendente, limitadas por fonte.
        """
        if not results:
            return []

        deduped = await self._deduplicate(results)
        logger.info(f"Deduplicacao: {len(results)} -> {len(deduped)}")

        clusters = await self._cluster(deduped)
        logger.info(f"Clusters formados: {len(clusters)}")

        synthesized = [self._merge_cluster(c) for c in clusters]
        synthesized.sort(key=lambda x: x.combined_score, reverse=True)
        return self._apply_source_cap(synthesized)

    # ── Deduplicacao ─────────────────────────────────────────────────────────

    async def _deduplicate(self, results: list[RankedResult]) -> list[RankedResult]:
        """Deduplica resultados em duas fases: lexica (rapida) + fuzzy (opcional).

        A fase lexica (`Deduplicator`, baseada em URL normalizada + entidade do
        titulo + SequenceMatcher) roda sempre e preserva o comportamento original.
        A fase fuzzy adicional (rapidfuzz) captura duplicatas que a fase lexica
        deixa passar (ex: mesmo item com titulo reescrito por fontes diferentes).
        """
        deduped = self.deduplicator.deduplicate(results)
        if len(deduped) <= 1:
            return deduped
        if len(deduped) > _FUZZY_DEDUPE_MAX_ITEMS:
            logger.debug(
                f"Fuzzy dedupe pulado: {len(deduped)} itens acima do limite de "
                f"custo ({_FUZZY_DEDUPE_MAX_ITEMS})"
            )
            return deduped
        return await self._fuzzy_dedupe(deduped)

    async def _fuzzy_dedupe(self, results: list[RankedResult]) -> list[RankedResult]:
        """Segundo passo de deduplicacao por similaridade fuzzy de texto.

        Compara todos os pares restantes com `rapidfuzz.fuzz.token_sort_ratio`
        (titulo e, em caso de mesma fonte, descricao). Os pares sao particionados
        em lotes e escaneados em paralelo via thread pool (a extensao C do
        rapidfuzz libera o GIL durante a comparacao, entao o paralelismo real
        aplica). Duplicatas sao unidas com union-find e apenas a primeira
        ocorrencia de cada grupo e mantida (mesma semantica do dedupe lexico).

        Se `rapidfuzz` nao estiver instalado, retorna os resultados inalterados.
        """
        try:
            from rapidfuzz import fuzz
        except ImportError:
            logger.debug("rapidfuzz indisponivel; usando apenas dedupe lexico")
            return results

        n = len(results)
        pairs = list(itertools.combinations(range(n), 2))
        if not pairs:
            return results

        chunks = [
            pairs[i : i + _FUZZY_PAIR_CHUNK_SIZE]
            for i in range(0, len(pairs), _FUZZY_PAIR_CHUNK_SIZE)
        ]
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(
                None, self._scan_fuzzy_chunk, chunk, results, fuzz
            )
            for chunk in chunks
        ]
        chunk_results = await asyncio.gather(*tasks)

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for dup_pairs in chunk_results:
            for i, j in dup_pairs:
                union(i, j)

        keep: dict[int, int] = {}
        for idx in range(n):
            root = find(idx)
            if root not in keep:
                keep[root] = idx

        merged = [results[idx] for idx in sorted(keep.values())]
        if len(merged) < n:
            logger.info(f"Fuzzy dedupe: {n} -> {len(merged)}")
        return merged

    @staticmethod
    def _scan_fuzzy_chunk(
        pairs: list[tuple[int, int]], results: list[RankedResult], fuzz: Any
    ) -> list[tuple[int, int]]:
        """Escaneia um lote de pares de indices e retorna os pares duplicados."""
        dup: list[tuple[int, int]] = []
        for i, j in pairs:
            a, b = results[i], results[j]
            title_score = fuzz.token_sort_ratio(a.title or "", b.title or "")
            if title_score >= _FUZZY_DEDUPE_THRESHOLD:
                dup.append((i, j))
                continue
            if a.source == b.source and a.description and b.description:
                desc_score = fuzz.token_sort_ratio(
                    a.description[:200], b.description[:200]
                )
                if desc_score >= _FUZZY_DEDUPE_THRESHOLD:
                    dup.append((i, j))
        return dup

    # ── Clusterizacao ────────────────────────────────────────────────────────

    async def _cluster(self, results: list[RankedResult]) -> list[list[RankedResult]]:
        """Agrupa resultados por entidade, via embeddings com fallback lexico."""
        if not results:
            return []
        if await self._ensure_embedder():
            try:
                return await self._cluster_by_embedding(results)
            except Exception as e:
                logger.warning(
                    f"Clusterizacao semantica falhou ({e}); usando fallback lexico"
                )
        return self._cluster_by_entity(results)

    async def _ensure_embedder(self) -> bool:
        """Carrega o modelo de embeddings de forma lazy e thread-safe (uma vez)."""
        if self._embedder_available is not None:
            return self._embedder_available
        async with self._embedder_lock:
            if self._embedder_available is not None:
                return self._embedder_available
            try:
                from sentence_transformers import SentenceTransformer

                logger.info(
                    f"Synthesizer: carregando modelo de embeddings {_EMBEDDING_MODEL_NAME}"
                )
                loop = asyncio.get_running_loop()
                self._embedder = await loop.run_in_executor(
                    None, lambda: SentenceTransformer(_EMBEDDING_MODEL_NAME)
                )
                self._embedder_available = True
                logger.info("Synthesizer: modelo de embeddings carregado com sucesso")
            except Exception as e:
                self._embedder_available = False
                logger.info(
                    f"Synthesizer: embeddings indisponiveis ({e}); "
                    "usando clusterizacao lexica"
                )
        return self._embedder_available

    async def _cluster_by_embedding(
        self, results: list[RankedResult]
    ) -> list[list[RankedResult]]:
        """Clusteriza resultados por similaridade semantica (cosine) de embeddings."""
        texts = [self._doc_text(r) for r in results]
        embeddings = await self._encode_in_chunks(texts)
        return self._greedy_cluster(results, embeddings)

    @staticmethod
    def _doc_text(r: RankedResult) -> str:
        """Monta o texto representativo (titulo + inicio da descricao) para embedding."""
        desc = (r.description or "")[:300]
        return f"{r.title or ''} {desc}".strip()

    async def _encode_in_chunks(self, texts: list[str]):
        """Codifica textos em embeddings, em lotes processados em paralelo.

        Cada lote roda em uma thread do executor padrao: as operacoes numericas
        do sentence-transformers/torch liberam o GIL durante o encode, entao
        varios lotes avancam de fato em paralelo em vez de serializar.
        """
        import numpy as np

        loop = asyncio.get_running_loop()
        chunks = [
            texts[i : i + _EMBED_CHUNK_SIZE]
            for i in range(0, len(texts), _EMBED_CHUNK_SIZE)
        ]
        tasks = [
            loop.run_in_executor(
                None,
                functools.partial(self._embedder.encode, chunk, show_progress_bar=False),
            )
            for chunk in chunks
        ]
        chunk_embeddings = await asyncio.gather(*tasks)
        return np.concatenate(chunk_embeddings, axis=0)

    @staticmethod
    def _greedy_cluster(
        results: list[RankedResult], embeddings
    ) -> list[list[RankedResult]]:
        """Clusterizacao incremental (leader/online) por similaridade de cosseno.

        Para cada item, compara com os centroides dos clusters existentes e
        entra no mais proximo se a similaridade >= threshold; senao, abre um
        cluster novo. O(n * k) com k = numero de clusters, sem depender de
        libs externas de ML alem do numpy (ja trazido pelo sentence-transformers).
        """
        import numpy as np

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        normed = embeddings / np.clip(norms, 1e-8, None)

        clusters: list[list[int]] = []
        centroids: list[Any] = []
        for idx, vec in enumerate(normed):
            best_idx, best_sim = -1, 0.0
            for c_idx, centroid in enumerate(centroids):
                sim = float(np.dot(vec, centroid))
                if sim > best_sim:
                    best_sim, best_idx = sim, c_idx
            if best_idx != -1 and best_sim >= _SEMANTIC_SIMILARITY_THRESHOLD:
                members = clusters[best_idx]
                members.append(idx)
                updated = centroids[best_idx] * (len(members) - 1) + vec
                centroids[best_idx] = updated / (np.linalg.norm(updated) + 1e-8)
            else:
                clusters.append([idx])
                centroids.append(vec.copy())

        return [[results[i] for i in members] for members in clusters]

    def _cluster_by_entity(
        self, results: list[RankedResult]
    ) -> list[list[RankedResult]]:
        """Agrupa resultados por entidade extraida do título (fallback lexico).

        Args:
            results: Lista de resultados deduplicados.

        Returns:
            list[list[RankedResult]]: Clusters, um por entidade identificada.
        """
        clusters: dict[str, list[RankedResult]] = defaultdict(list)
        for r in results:
            entity = self._extract_entity(r.title)
            clusters[entity].append(r)
        return list(clusters.values())

    def _extract_entity(self, title: str) -> str:
        """Extrai a entidade principal de um titulo para uso como chave de cluster.

        Args:
            title: Título do resultado de busca.

        Returns:
            str: Palavra-chave ou slug da entidade, ou ``"unknown"`` se vazio.
        """
        title = (title or "").lower().strip()
        import re

        title = re.sub(r"^(show hn:|ask hn:|tell hn:)\s*", "", title)
        words = title.split()
        if not words:
            return "unknown"
        if "/" in words[0]:
            return words[0].split("/")[-1]
        for w in words:
            clean = w.strip(".,;:!?()[]{}\"'")
            if clean and clean not in _STOPWORDS:
                return clean
        return words[0]

    # ── Mesclagem e ordenacao (inalterado) ──────────────────────────────────

    @staticmethod
    def _compute_verdict(
        score: float, description: str, highlights: list[str]
    ) -> tuple[str, str, str, int]:
        """Retorna (verdict, tldr, next_step, read_min) a partir do score e conteúdo."""
        if score >= 75:
            verdict = Verdict.FOCA.value
            next_step = "Avaliar e testar esta semana — alta relevância confirmada por múltiplas fontes."
        elif score >= 50:
            verdict = Verdict.CONSIDERA.value
            next_step = "Agendar leitura quando possível — relevância contextual, sem urgência imediata."
        elif score >= 30:
            verdict = Verdict.ACOMPANHA.value
            next_step = "Marcar para revisão futura — tangencial ao tema principal."
        else:
            verdict = Verdict.IGNORA.value
            next_step = "Dispensar por ora — fora do escopo da pesquisa atual."

        # tldr: combina description truncada com o highlight mais forte
        desc_short = (
            (description[:120] + "…") if len(description) > 120 else description
        )
        if highlights:
            tldr = f"{desc_short} [{highlights[0]}]"
        else:
            tldr = desc_short

        # read_min: estimativa por tamanho do texto disponível (2-10 min)
        total_chars = len(description) + sum(len(h) for h in highlights)
        read_min = max(2, min(10, round(total_chars / 600)))

        return verdict, tldr, next_step, read_min

    def _merge_cluster(self, cluster: list[RankedResult]) -> SynthesizedResult:
        """Mescla um cluster de resultados da mesma entidade em um unico `SynthesizedResult`.

        Consolida titulos, descricoes, metricas, scores e gera highlights,
        veredicto, TL;DR e estimativa de leitura.

        Args:
            cluster: Lista de `RankedResult` da mesma entidade.

        Returns:
            SynthesizedResult: Resultado consolidado com todos os metadados.
        """
        entity = self._extract_entity(cluster[0].title)
        best_title = max(cluster, key=lambda x: len(x.title)).title

        descriptions = [r.description for r in cluster if r.description]
        best_description = descriptions[0] if descriptions else ""

        sources = list(set(r.source for r in cluster))
        urls = list(set(r.url for r in cluster))

        scores = [r.score for r in cluster]
        combined_score = round(sum(scores) / len(scores), 2)

        merged_metrics: dict = {}
        for r in cluster:
            for key, value in r.metrics.items():
                if key not in merged_metrics:
                    merged_metrics[key] = value
                elif isinstance(value, (int, float)) and isinstance(
                    merged_metrics[key], (int, float)
                ):
                    merged_metrics[key] = max(merged_metrics[key], value)

        highlights = []
        if merged_metrics.get("stars", 0) > 1000:
            highlights.append(f"{merged_metrics['stars']} stars no GitHub")
        if merged_metrics.get("upvotes", 0) > 100:
            highlights.append(f"{merged_metrics['upvotes']} upvotes no Reddit")
        if merged_metrics.get("points", 0) > 50:
            highlights.append(f"{merged_metrics['points']} points no HN")

        dates = [r.fetched_at for r in cluster]
        first_seen = min(dates)
        last_seen = max(dates)

        verdict, tldr, next_step, read_min = self._compute_verdict(
            combined_score, best_description, highlights
        )

        best_item = max(cluster, key=lambda x: x.score)

        return SynthesizedResult(
            entity=entity,
            title=best_title,
            description=best_description,
            sources=sources,
            urls=urls,
            combined_score=combined_score,
            metrics=merged_metrics,
            highlights=highlights,
            first_seen=first_seen,
            last_seen=last_seen,
            verdict=verdict,
            tldr=tldr,
            next_step=next_step,
            read_min=read_min,
            evidence_quality=getattr(best_item, "evidence_quality", "unknown"),
            hallucination_flags=getattr(best_item, "hallucination_flags", []),
        )

    def _apply_source_cap(
        self, results: list[SynthesizedResult], max_per_source: int = 10
    ) -> list[SynthesizedResult]:
        """Cap per-source results to avoid one source dominating.
        Always keeps the global top-20 by combined_score.
        """
        source_counts: dict[str, int] = defaultdict(int)
        filtered = []
        for r in results:
            primary_source = r.sources[0] if r.sources else "unknown"
            if source_counts[primary_source] < max_per_source:
                source_counts[primary_source] += 1
                filtered.append(r)
        # Guarantee at least top-20 even if all from same source
        if len(filtered) < 20 and len(results) > len(filtered):
            existing_ids = {id(r) for r in filtered}
            for r in results:
                if id(r) not in existing_ids:
                    filtered.append(r)
                if len(filtered) >= 20:
                    break
        return filtered
