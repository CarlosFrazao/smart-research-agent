from typing import List, Dict
from collections import defaultdict
from datetime import datetime
from src.types import RankedResult, SynthesizedResult, Verdict
from src.utils.deduplicator import Deduplicator
from src.clients.llm_client import LLMClient
import logging

logger = logging.getLogger(__name__)

_STOPWORDS = {"the", "a", "an", "is", "are", "best", "top", "new", "open"}


class Synthesizer:
    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client
        self.deduplicator = Deduplicator()

    async def synthesize(self, results: List[RankedResult]) -> List[SynthesizedResult]:
        deduped = self.deduplicator.deduplicate(results)
        logger.info(f"Deduplicacao: {len(results)} -> {len(deduped)}")

        clusters = self._cluster_by_entity(deduped)
        logger.info(f"Clusters formados: {len(clusters)}")

        synthesized = [self._merge_cluster(c) for c in clusters]
        synthesized.sort(key=lambda x: x.combined_score, reverse=True)
        return self._apply_source_cap(synthesized)

    def _cluster_by_entity(
        self, results: List[RankedResult]
    ) -> List[List[RankedResult]]:
        clusters: Dict[str, List[RankedResult]] = defaultdict(list)
        for r in results:
            entity = self._extract_entity(r.title)
            clusters[entity].append(r)
        return list(clusters.values())

    def _extract_entity(self, title: str) -> str:
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

    @staticmethod
    def _compute_verdict(score: float, description: str, highlights: List[str]) -> tuple[str, str, str, int]:
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
        desc_short = (description[:120] + "…") if len(description) > 120 else description
        if highlights:
            tldr = f"{desc_short} [{highlights[0]}]"
        else:
            tldr = desc_short

        # read_min: estimativa por tamanho do texto disponível (2-10 min)
        total_chars = len(description) + sum(len(h) for h in highlights)
        read_min = max(2, min(10, round(total_chars / 600)))

        return verdict, tldr, next_step, read_min

    def _merge_cluster(self, cluster: List[RankedResult]) -> SynthesizedResult:
        entity = self._extract_entity(cluster[0].title)
        best_title = max(cluster, key=lambda x: len(x.title)).title

        descriptions = [r.description for r in cluster if r.description]
        best_description = descriptions[0] if descriptions else ""

        sources = list(set(r.source for r in cluster))
        urls = list(set(r.url for r in cluster))

        scores = [r.score for r in cluster]
        combined_score = round(sum(scores) / len(scores), 2)

        merged_metrics: Dict = {}
        for r in cluster:
            for key, value in r.metrics.items():
                if key not in merged_metrics:
                    merged_metrics[key] = value
                elif isinstance(value, (int, float)) and isinstance(merged_metrics[key], (int, float)):
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
        self, results: List[SynthesizedResult], max_per_source: int = 10
    ) -> List[SynthesizedResult]:
        """Cap per-source results to avoid one source dominating.
        Always keeps the global top-20 by combined_score.
        """
        source_counts: Dict[str, int] = defaultdict(int)
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
