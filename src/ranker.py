import math
from datetime import datetime
from typing import List, Dict, Any
from src.types import SearchResult, RankedResult
from src.clients.llm_client import LLMClient
from src.misinformation_detector import MisinformationDetector
import logging

logger = logging.getLogger(__name__)


class QualityRanker:
    def __init__(self, llm_client: LLMClient = None, config: Dict[str, Any] = None):
        self.llm = llm_client
        self.config = config or {}
        self.detector = MisinformationDetector()

    def _recency_score(self, date_str: str) -> float:
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
        return 50.0

    async def rank(self, results: List[SearchResult]) -> List[RankedResult]:
        ranked = []
        for result in results:
            if result.source == "github":
                score = self._github_score(result)
            elif result.source == "reddit":
                score = self._reddit_score(result)
            elif result.source == "hackernews":
                score = self._hn_score(result)
            else:
                score = self._generic_score(result)

            is_flagged, penalty, reason = self.detector.check_url(result.url)
            final_score = score
            if is_flagged:
                final_score = round(score * penalty, 2)

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
                        "base_score": score,
                        "misinformation_penalty": penalty if is_flagged else 1.0,
                        "misinformation_reason": reason if is_flagged else "",
                    },
                )
            )

        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked
