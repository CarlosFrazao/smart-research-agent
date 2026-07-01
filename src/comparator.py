"""
comparator.py — Side-by-Side Comparator (Bloco 4.3)

Detects comparison queries (X vs Y, A ou B, etc.), extracts the entities
being compared, aggregates result metrics per entity, and renders a
formatted Markdown comparison table for the final report.

Features
--------
- `detect_comparison_query` — regex-based detection + entity extraction
- `build_entity_profiles` — aggregates score, sources, stars, recency, sentiment
- `generate_comparison_section` — full Markdown section with summary + table
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from src.types import SynthesizedResult

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Patterns that indicate a comparative query
_COMPARISON_PATTERNS = [
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bou\b",       # Portuguese: "A ou B"
    r"\bor\b",
    r"\bx\b",        # common in PT: "Python x Java"
    r"\bcompar[ae]",
    r"\bmelhor\b",   # "qual é melhor"
    r"\bbetter\b",
    r"\bdiferen[çc]a",
    r"\bdifference\b",
]

_COMPARISON_RE = re.compile(
    "|".join(_COMPARISON_PATTERNS),
    re.IGNORECASE,
)

# Splitters that separate the two sides of a comparison
_SPLITTER_RE = re.compile(
    r"\s+(?:vs\.?|versus|ou|or|x)\s+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EntityProfile:
    """Aggregated profile for one entity in a comparison."""
    name: str
    result_count: int = 0
    avg_score: float = 0.0
    sources: List[str] = field(default_factory=list)
    total_stars: int = 0
    top_titles: List[str] = field(default_factory=list)
    avg_recency: float = 0.0   # 0-1 normalised
    avg_sentiment: float = 0.0  # -1 to +1


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class Comparator:
    """Detects comparison queries and builds side-by-side comparison reports."""

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def detect_comparison_query(
        self, query: str
    ) -> Tuple[bool, List[str]]:
        """
        Check whether the query is a comparison and extract the entities.

        Parameters
        ----------
        query : str
            Raw user query string.

        Returns
        -------
        (is_comparison, entities)
            `is_comparison` is True when a comparison pattern is found.
            `entities` is a list of 2+ entity strings (may be empty if
            extraction fails, even though is_comparison is True).
        """
        if not query or not query.strip():
            return False, []

        is_comparison = bool(_COMPARISON_RE.search(query))
        if not is_comparison:
            return False, []

        entities = self._extract_entities(query)
        return True, entities

    def build_entity_profiles(
        self,
        entities: List[str],
        results: List[SynthesizedResult],
    ) -> List[EntityProfile]:
        """
        For each entity, aggregate stats from the results that mention it.

        Matching is case-insensitive substring match on title + snippet.
        """
        profiles: List[EntityProfile] = []
        for entity in entities:
            profile = EntityProfile(name=entity)
            entity_lower = entity.lower()
            matched: List[SynthesizedResult] = []

            for r in results:
                urls_str = " ".join(r.urls) if hasattr(r, "urls") and r.urls else ""
                desc = r.description if hasattr(r, "description") else ""
                text = " ".join(
                    filter(None, [r.title, desc, urls_str])
                ).lower()
                if entity_lower in text:
                    matched.append(r)

            if not matched:
                profiles.append(profile)
                continue

            profile.result_count = len(matched)
            scores = []
            for r in matched:
                sc = getattr(r, "combined_score", None)
                if sc is None:
                    # Fallback caso seja um objeto mockado antigo ou parcial
                    sc = getattr(r, "score", 0.0)
                if sc is not None:
                    scores.append(sc)
            profile.avg_score = sum(scores) / len(scores) if scores else 0.0

            # Deduplicated source list
            seen: set = set()
            for r in matched:
                for s in r.sources:
                    if s not in seen:
                        profile.sources.append(s)
                        seen.add(s)

            # Stars (GitHub)
            stars_list = [
                r.metrics.get("stars", 0) for r in matched if r.metrics.get("stars")
            ]
            profile.total_stars = sum(stars_list)

            # Top titles (up to 3)
            profile.top_titles = [
                r.title for r in matched[:3] if r.title
            ]

            # Recency (0-1 stored in metrics as "recency_score")
            recency_list = [
                r.metrics.get("recency_score", 0.5) for r in matched
            ]
            profile.avg_recency = (
                sum(recency_list) / len(recency_list) if recency_list else 0.5
            )

            # Sentiment (if stored by SentimentAnalyzer in metrics)
            sentiment_list = [
                r.metrics.get("sentiment_score", 0.0) for r in matched
            ]
            profile.avg_sentiment = (
                sum(sentiment_list) / len(sentiment_list) if sentiment_list else 0.0
            )

            profiles.append(profile)

        return profiles

    def generate_comparison_section(
        self,
        query: str,
        results: List[SynthesizedResult],
    ) -> str:
        """
        Full Markdown section: header + summary prose + comparison table.

        Returns an empty string if the query is not comparative.
        """
        is_comparison, entities = self.detect_comparison_query(query)
        if not is_comparison or len(entities) < 2:
            return ""

        profiles = self.build_entity_profiles(entities, results)

        lines: List[str] = [
            "## ⚖️ Comparação Side-by-Side",
            "",
            f"A query **\"{query}\"** é comparativa. Abaixo um confronto direto entre as opções identificadas:",
            "",
        ]

        # Build comparison table
        # Headers — one column per entity
        header_cells = ["Critério"] + [f"**{p.name}**" for p in profiles]
        separator = ["---"] + ["---"] * len(profiles)
        lines.append("| " + " | ".join(header_cells) + " |")
        lines.append("| " + " | ".join(separator) + " |")

        # Row helper
        def row(label: str, *values: str) -> str:
            return "| " + " | ".join([label] + list(values)) + " |"

        # Results count
        lines.append(row(
            "📊 Resultados encontrados",
            *[str(p.result_count) for p in profiles],
        ))

        # Avg score
        lines.append(row(
            "🏆 Score médio",
            *[f"`{p.avg_score:.2f}`" for p in profiles],
        ))

        # Stars
        lines.append(row(
            "⭐ Stars (GitHub)",
            *[f"{p.total_stars:,}" if p.total_stars else "—" for p in profiles],
        ))

        # Recency
        lines.append(row(
            "🕐 Recência",
            *[_recency_label(p.avg_recency) for p in profiles],
        ))

        # Sentiment
        lines.append(row(
            "💬 Sentimento da comunidade",
            *[_sentiment_label(p.avg_sentiment) for p in profiles],
        ))

        # Sources
        lines.append(row(
            "🔗 Fontes",
            *[", ".join(p.sources[:4]) or "—" for p in profiles],
        ))

        lines.append("")

        # Winner summary
        winner = self._pick_winner(profiles)
        if winner:
            lines += [
                f"> 💡 **Recomendação preliminar:** com base no volume de resultados, score e sentimento da comunidade, **{winner}** apresenta vantagem nesta análise.",
                "",
            ]

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_entities(query: str) -> List[str]:
        """
        Split the query on comparison keywords and return cleaned tokens.

        Strategy:
        1. Try to split on explicit 'vs', 'ou', 'or', 'x', 'versus'.
        2. Fall back to extracting the two longest capitalised sequences
           or the two longest contiguous non-stopword sequences.
        """
        # Remove leading comparison qualifiers like "qual é melhor ... vs ..."
        clean_query = re.sub(
            r"^(?:qual\s+[eé]\s+(?:o\s+)?melhor\s*[,:]?\s*|what\s+is\s+(?:the\s+)?better\s*[,:]?\s*)",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()

        parts = _SPLITTER_RE.split(clean_query)
        if len(parts) >= 2:
            entities = [_clean_entity(p) for p in parts]
            return [e for e in entities if e]

        # Fallback: split on "ou" or "or" anywhere
        fallback = re.split(r"\s+(?:ou|or)\s+", clean_query, flags=re.IGNORECASE)
        if len(fallback) >= 2:
            return [_clean_entity(p) for p in fallback if _clean_entity(p)]

        return []

    @staticmethod
    def _pick_winner(profiles: List[EntityProfile]) -> Optional[str]:
        """Return the name of the profile with the highest composite score."""
        if not profiles:
            return None

        def composite(p: EntityProfile) -> float:
            return (
                p.avg_score * 0.5
                + p.result_count * 0.02
                + p.avg_recency * 0.2
                + p.avg_sentiment * 0.1
                + (min(p.total_stars, 100_000) / 100_000) * 0.2
            )

        ranked = sorted(profiles, key=composite, reverse=True)
        # Only declare a winner if there is a meaningful gap
        if len(ranked) >= 2 and composite(ranked[0]) > composite(ranked[1]) * 1.05:
            return ranked[0].name
        return None


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def _recency_label(score: float) -> str:
    if score >= 0.75:
        return "🟢 Recente"
    if score >= 0.4:
        return "🟡 Moderado"
    return "🔴 Desatualizado"


def _sentiment_label(score: float) -> str:
    if score > 0.2:
        return "😊 Positivo"
    if score < -0.2:
        return "😞 Negativo"
    return "😐 Neutro"


def _clean_entity(text: str) -> str:
    """Strip punctuation and extraneous whitespace from an extracted entity."""
    text = text.strip(" ,.:;!?\"'")
    # Remove common leading articles (PT + EN)
    text = re.sub(r"^(?:o|a|os|as|um|uma|the|an?)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()
