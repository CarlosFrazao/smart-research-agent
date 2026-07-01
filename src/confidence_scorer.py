import re
from typing import List, Dict
from src.types import SearchResult
import logging

logger = logging.getLogger(__name__)

_TRUSTED_DOMAINS = frozenset({
    "github.com", "arxiv.org", "reddit.com", "news.ycombinator.com",
    "stackoverflow.com", "docs.python.org", "developer.mozilla.org",
    "pypi.org", "npmjs.com", "pkg.go.dev", "crates.io",
    "microsoft.com", "google.com", "openai.com", "anthropic.com",
    "huggingface.co", "pytorch.org", "tensorflow.org",
})

_UNTRUSTED_DOMAINS = frozenset({
    "medium.com", "buzzfeed.com", "quora.com",
    "pinterest.com", "slideshare.net",
})

_CLICKBAIT_PATTERNS = re.compile(
    r"\b(you won\'t believe|shocking|secret|hack|trick|amazing|"
    r"incredible|unbelievable|mind.blowing|click here|must.?see)\b",
    re.IGNORECASE,
)

_ABSOLUTE_CLAIM_PATTERNS = re.compile(
    r"\b(you won\'t believe|shocking|secret|hack|trick|amazing|"
    r"incredible|unbelievable|mind.blowing|worst|worst|"
    r"único|definitivo|perfeito|exclusivo)\b",
    re.IGNORECASE,
)

_DATE_PATTERN = re.compile(
    r"\b(20\d{2}[-/]\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)

_URL_PATTERN = re.compile(r"https?://[^\s\"\'<>]+")

_REPETITION_THRESHOLD = 0.30


class ConfidenceScorer:
    """
    Assigns a confidence_score (0.0–1.0) and evidence_quality to each SearchResult.

    Inspired by Clarity Research anti-hallucination approach:
    every claim should be traceable to real, verifiable evidence.
    """

    async def score_result(self, result: SearchResult) -> SearchResult:
        """Scores a single SearchResult and returns it with confidence fields filled."""
        score: float = 0.5
        flags: List[str] = []
        content = result.description or ""
        url = result.url or ""
        word_count = len(content.split())

        domain = self._extract_domain(url)

        if domain in _TRUSTED_DOMAINS:
            score += 0.20
        elif domain in _UNTRUSTED_DOMAINS:
            score -= 0.20
            flags.append("untrusted_domain")

        # GitHub repos and HN posts typically have short descriptions — don't penalize
        CODE_SOURCES = {"github", "hackernews", "awesome"}
        if word_count >= 300:
            score += 0.15
        elif word_count < 10 and result.source not in CODE_SOURCES:
            score -= 0.30
            flags.append("content_too_short")
        elif word_count < 50 and result.source not in CODE_SOURCES:
            score -= 0.10
            flags.append("content_brief")

        # Bonus for code/community sources with engagement metrics
        if result.source == "github":
            stars = result.metrics.get("stars", 0)
            if stars > 100:
                score += 0.10
            elif stars > 10:
                score += 0.05

        if _DATE_PATTERN.search(content):
            score += 0.15

        cited_urls = _URL_PATTERN.findall(content)
        if len(cited_urls) >= 1:
            score += 0.10
            result.citations = cited_urls[:10]

        if not _CLICKBAIT_PATTERNS.search(result.title or ""):
            score += 0.10
        else:
            flags.append("clickbait_title")

        ranker_score = result.metrics.get("score", result.metrics.get("stars", 0))
        if isinstance(ranker_score, (int, float)) and ranker_score > 70:
            score += 0.10

        if self._has_repetition(content):
            score -= 0.10
            flags.append("repetitive_content")

        if _ABSOLUTE_CLAIM_PATTERNS.search(result.title or ""):
            score -= 0.15
            flags.append("absolute_claim_detected")

        score = max(0.0, min(1.0, score))

        result.confidence_score = round(score, 3)
        result.hallucination_flags = flags
        result.evidence_quality = self._classify_evidence_quality(score)

        return result

    async def score_batch(
        self,
        results: List[SearchResult],
        cross_validate: bool = True,
    ) -> List[SearchResult]:
        """
        Scores a list of SearchResults.
        When cross_validate=True, also checks for contradictions between results.
        """
        scored = [await self.score_result(r) for r in results]

        if cross_validate and len(scored) > 1:
            contradictions_map = self._detect_contradictions(scored)
            for result in scored:
                if result.url in contradictions_map:
                    result.contradictions = contradictions_map[result.url]
                    if "contradicted_by_other_sources" not in result.hallucination_flags:
                        result.hallucination_flags.append("contradicted_by_other_sources")
                    result.confidence_score = round(
                        max(0.0, result.confidence_score - 0.10), 3
                    )

        return scored

    def _detect_contradictions(
        self, results: List[SearchResult]
    ) -> Dict[str, List[str]]:
        """
        Detects when two results make opposing claims about the same subject.
        Uses simple heuristic: same title keywords + opposing sentiment signals.
        Returns {result_url: [urls_that_contradict_it]}.
        """
        positive_signals = re.compile(
            r"\b(fast|better|best|recommended|popular|reliable|stable|"
            r"rápido|melhor|recomendado|popular|confiável|estável)\b",
            re.IGNORECASE,
        )
        negative_signals = re.compile(
            r"\b(slow|worse|worst|avoid|broken|deprecated|buggy|"
            r"lento|pior|evitar|quebrado|descontinuado|problemático)\b",
            re.IGNORECASE,
        )

        contradictions: Dict[str, List[str]] = {}

        for i, r1 in enumerate(results):
            for j, r2 in enumerate(results):
                if i >= j or r1.url == r2.url:
                    continue

                keywords_1 = set(re.findall(r"\b\w{4,}\b", (r1.title or "").lower()))
                keywords_2 = set(re.findall(r"\b\w{4,}\b", (r2.title or "").lower()))
                overlap = keywords_1 & keywords_2

                if len(overlap) < 1:
                    continue

                r1_positive = bool(positive_signals.search(r1.description or ""))
                r1_negative = bool(negative_signals.search(r1.description or ""))
                r2_positive = bool(positive_signals.search(r2.description or ""))
                r2_negative = bool(negative_signals.search(r2.description or ""))

                if (r1_positive and r2_negative) or (r1_negative and r2_positive):
                    contradictions.setdefault(r1.url, []).append(r2.url)
                    contradictions.setdefault(r2.url, []).append(r1.url)

        return contradictions

    def _extract_domain(self, url: str) -> str:
        """Extracts the bare domain (e.g. 'github.com') from a URL."""
        match = re.search(r"https?://(?:www\.)?([^/\s?#]+)", url)
        return match.group(1).lower() if match else ""

    def _has_repetition(self, text: str) -> bool:
        """Returns True when repeated phrases exceed REPETITION_THRESHOLD of the text."""
        if not text:
            return False
        words = text.lower().split()
        if len(words) < 20:
            return False
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
        if not bigrams:
            return False
        unique_ratio = len(set(bigrams)) / len(bigrams)
        return unique_ratio < (1.0 - _REPETITION_THRESHOLD)

    def _classify_evidence_quality(self, score: float) -> str:
        """Maps numeric score to evidence quality label."""
        if score >= 0.75:
            return "verified"
        elif score >= 0.55:
            return "cited"
        elif score >= 0.35:
            return "inferred"
        return "unknown"
