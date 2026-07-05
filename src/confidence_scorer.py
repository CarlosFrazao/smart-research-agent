"""Scorer de confianca de resultados de pesquisa usando heuristicas multi-dimensionais e LLM.

O LLM (quando injetado) e usado de forma seletiva e bounded: apenas para refinar a
classificacao de tipo de afirmacao (fact/opinion/statistics) de conteudo textual
ambiguo. Fontes estruturadas orientadas a API (github, hackernews, awesome) e
casos onde a heuristica ja classifica com confianca nunca acionam o LLM — o
scoring nesses casos permanece 100% heuristico, sem custo de rede/tokens.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, UTC

from src.types import SearchResult

logger = logging.getLogger(__name__)

# Fontes com metadados objetivos vindos de API (contagens, votos, etc.) em vez de
# prosa livre: nao ha o que uma classificacao de "tipo de afirmacao" agregaria aqui,
# entao o scoring dessas fontes e sempre puramente heuristico (nunca aciona o LLM).
_STRUCTURED_SOURCES = frozenset({"github", "hackernews", "awesome"})


# --- Padrões do V1 ---
_TRUSTED_DOMAINS = frozenset(
    {
        "github.com",
        "arxiv.org",
        "reddit.com",
        "news.ycombinator.com",
        "stackoverflow.com",
        "docs.python.org",
        "developer.mozilla.org",
        "pypi.org",
        "npmjs.com",
        "pkg.go.dev",
        "crates.io",
        "microsoft.com",
        "google.com",
        "openai.com",
        "anthropic.com",
        "huggingface.co",
        "pytorch.org",
        "tensorflow.org",
    }
)

_UNTRUSTED_DOMAINS = frozenset(
    {
        "medium.com",
        "buzzfeed.com",
        "quora.com",
        "pinterest.com",
        "slideshare.net",
    }
)

_CLICKBAIT_PATTERNS = re.compile(
    r"\b(you won\'t believe|shocking|secret|hack|trick|amazing|"
    r"incredible|unbelievable|mind.blowing|click here|must.?see)\b",
    re.IGNORECASE,
)

_ABSOLUTE_CLAIM_PATTERNS = re.compile(
    r"\b(worst|best|único|definitivo|perfeito|exclusivo)\b",
    re.IGNORECASE,
)

_DATE_PATTERN = re.compile(
    r"\b(20\d{2}[-/]\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)

_URL_PATTERN = re.compile(r"https?://[^\s\"\'<>]+")
_REPETITION_THRESHOLD = 0.30

# --- Padrões do V2 ---
_FACT_PATTERNS = re.compile(
    r"\b(?:according\s+to|estudos\s+mostram|pesquisa\s+indica|dados\s+de|statistics\s+show|survey\s+found|report\s+says|in\s+\d{4}|em\s+\d{4})\b|"
    r"\b\d+(?:[\.,]\d+)?\s*%|"
    r"\b\d+\s+(?:million|billion|thousand|milhões|bilhões)\b|"
    r"\b(?:source|fonte|referência|published|publicado)\b",
    re.IGNORECASE,
)

_OPINION_PATTERNS = re.compile(
    r"\b(?:I\s+think|I\s+believe|in\s+my\s+opinion|acredito|acho\s+que|na\s+minha\s+opinião)\b|"
    r"\b(?:arguably|seems\s+to|appears\s+to|might\s+be|could\s+be)\b|"
    r"\b(?:many\s+people\s+think|some\s+argue|critics\s+say|defensores\s+argumentam)\b",
    re.IGNORECASE,
)

_STATISTICS_PATTERNS = re.compile(
    r"\b\d+(?:[\.,]\d+)?\s*%|"
    r"\b\d+\s*(?:percent|porcento)\b|"
    r"\b\d+\s*(?:users|utilizadores|respondents|entrevistados)\b|"
    r"\b(?:median|average|mean|média|mediana|variância|desvio\s+padrão|correlation|correlação|p-value|statistical)\b",
    re.IGNORECASE,
)

_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_FRESHNESS_PENALTY_YEARS = 3

# --- Gate de uso do LLM (V2) ---
# O LLM so e chamado quando o tipo heuristico e "unknown" (nenhum padrao bateu) ou
# "opinion" (conteudo subjetivo por definicao). "fact"/"statistics" sao objetivos
# pelo proprio padrao que os classificou (%, citacao, data, "segundo/according to"),
# entao NUNCA acionam LLM — mesmo quando o numero de claim_confidence sai baixo, pois
# essa formula e deliberadamente conservadora (dilui por hits de outras categorias)
# e nao deve ser lida como "quao objetivo e o conteudo".
_LLM_ELIGIBLE_CLAIM_TYPES = frozenset({"unknown", "opinion"})
# Textos muito curtos nao tem sinal suficiente para justificar uma chamada de LLM.
_MIN_WORDS_FOR_LLM_CLASSIFICATION = 15


def _get_current_year() -> int:
    return datetime.now(UTC).year


class ConfidenceScorer:
    """
    Assigns a confidence_score (0.0–1.0) and evidence_quality to each SearchResult.

    Inspired by Clarity Research anti-hallucination approach:
    every claim should be traceable to real, verifiable evidence.
    """

    @property
    def current_year(self) -> int:
        from datetime import UTC, datetime
        return datetime.now(UTC).year

    @property
    def _current_year(self) -> int:
        return self.current_year

    async def score_result(self, result: SearchResult) -> SearchResult:
        """Scores a single SearchResult and returns it with confidence fields filled."""
        score: float = 0.5
        flags: list[str] = []
        content = result.description or ""
        url = result.url or ""
        word_count = len(content.split())

        domain = self._extract_domain(url)

        if domain in _TRUSTED_DOMAINS:
            score += 0.20
        elif domain in _UNTRUSTED_DOMAINS:
            score -= 0.20
            flags.append("untrusted_domain")

        if word_count >= 300:
            score += 0.15
        elif word_count < 10 and result.source not in _STRUCTURED_SOURCES:
            score -= 0.30
            flags.append("content_too_short")
        elif word_count < 50 and result.source not in _STRUCTURED_SOURCES:
            score -= 0.10
            flags.append("content_brief")

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
        results: list[SearchResult],
        cross_validate: bool = True,
    ) -> list[SearchResult]:
        """Scores a list of SearchResults."""
        scored = [await self.score_result(r) for r in results]

        if cross_validate and len(scored) > 1:
            contradictions_map = self._detect_contradictions(scored)
            for result in scored:
                if result.url in contradictions_map:
                    result.contradictions = contradictions_map[result.url]
                    if (
                        "contradicted_by_other_sources"
                        not in result.hallucination_flags
                    ):
                        result.hallucination_flags.append(
                            "contradicted_by_other_sources"
                        )
                    result.confidence_score = round(
                        max(0.0, result.confidence_score - 0.10), 3
                    )

        return scored

    def _detect_contradictions(
        self, results: list[SearchResult]
    ) -> dict[str, list[str]]:
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

        contradictions: dict[str, list[str]] = {}

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
        match = re.search(r"https?://(?:www\.)?([^/\s?#]+)", url)
        return match.group(1).lower() if match else ""

    def _has_repetition(self, text: str) -> bool:
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
        if score >= 0.75:
            return "verified"
        elif score >= 0.55:
            return "cited"
        elif score >= 0.35:
            return "inferred"
        return "unknown"


class ConfidenceScorerV2(ConfidenceScorer):
    """
    Extensão do ConfidenceScorer v1 com:
      - Classificação factual (fact/opinion/statistics) por heurística + LLM opcional
      - Detecção de circularidade de links entre fontes
      - Penalização por frescor de conteúdo
    """

    def __init__(self, llm_client=None):
        super().__init__()
        self.llm = llm_client

    async def score_result(self, result: SearchResult) -> SearchResult:
        result = await super().score_result(result)
        content = result.description or ""

        # Classificação Factual — heurística primeiro (grátis); LLM só se ambíguo
        claim_type, claim_confidence = self._classify_claim(content, result.title or "")
        claim_source = "heuristic"

        word_count = len(content.split())
        if (
            self.llm is not None
            and result.source not in _STRUCTURED_SOURCES
            and word_count >= _MIN_WORDS_FOR_LLM_CLASSIFICATION
            and claim_type in _LLM_ELIGIBLE_CLAIM_TYPES
        ):
            refined = await self._refine_claim_with_llm(content, result.title or "")
            if refined is not None:
                claim_type, claim_confidence = refined
                claim_source = "llm"

        result.metrics["claim_type"] = claim_type
        result.metrics["claim_confidence"] = claim_confidence
        result.metrics["claim_source"] = claim_source

        if claim_type == "fact":
            result.confidence_score = min(1.0, result.confidence_score + 0.08)
        elif claim_type == "opinion":
            result.confidence_score = max(0.0, result.confidence_score - 0.05)
            if "opinion_content" not in result.hallucination_flags:
                result.hallucination_flags.append("opinion_content")
        elif claim_type == "statistics":
            result.confidence_score = min(1.0, result.confidence_score + 0.12)

        # Frescor do Conteúdo
        freshness_score, freshness_year = self._calculate_freshness(content)
        result.metrics["freshness_year"] = freshness_year
        result.metrics["freshness_score"] = freshness_score

        if freshness_score < 0.5:
            penalty = round((0.5 - freshness_score) * 0.20, 3)
            result.confidence_score = max(0.0, result.confidence_score - penalty)
            if "stale_content" not in result.hallucination_flags:
                result.hallucination_flags.append("stale_content")

        result.confidence_score = round(max(0.0, min(1.0, result.confidence_score)), 3)
        result.evidence_quality = self._classify_evidence_quality(
            result.confidence_score
        )

        return result

    async def score_batch(
        self,
        results: list[SearchResult],
        cross_validate: bool = True,
        detect_circularity: bool = True,
    ) -> list[SearchResult]:
        # gather (nao sequencial): score_result pode agora fazer uma chamada LLM real
        # por item ambiguo, entao paralelizar evita empilhar N latencias de rede.
        # asyncio.gather preserva a ordem da lista de entrada.
        scored = list(await asyncio.gather(*(self.score_result(r) for r in results)))

        if cross_validate and len(scored) > 1:
            contradictions_map = self._detect_contradictions(scored)
            for result in scored:
                if result.url in contradictions_map:
                    result.contradictions = contradictions_map[result.url]
                    if (
                        "contradicted_by_other_sources"
                        not in result.hallucination_flags
                    ):
                        result.hallucination_flags.append(
                            "contradicted_by_other_sources"
                        )
                    result.confidence_score = round(
                        max(0.0, result.confidence_score - 0.10), 3
                    )

        if detect_circularity and len(scored) > 1:
            circular_groups = self._detect_link_circularity(scored)
            for result in scored:
                if result.url in circular_groups:
                    circular_partners = circular_groups[result.url]
                    result.metrics["circular_sources"] = circular_partners
                    if "circular_reference" not in result.hallucination_flags:
                        result.hallucination_flags.append("circular_reference")
                    result.confidence_score = round(
                        max(0.0, result.confidence_score - 0.07), 3
                    )
                    logger.info(
                        f"Circularidade detectada: {result.url[:60]} referencia {len(circular_partners)} parceiros"
                    )

        return scored

    def _classify_claim(self, content: str, title: str) -> tuple[str, float]:
        text = f"{title} {content}"
        stat_hits = sum(1 for _ in _STATISTICS_PATTERNS.finditer(text))
        fact_hits = sum(1 for _ in _FACT_PATTERNS.finditer(text))
        opinion_hits = sum(1 for _ in _OPINION_PATTERNS.finditer(text))

        total = stat_hits + fact_hits + opinion_hits

        if total == 0:
            return ("unknown", 0.4)

        if stat_hits > 0:
            confidence = min(1.0, (stat_hits * 1.5) / max(total, 1))
            return ("statistics", round(confidence, 2))
        elif fact_hits >= opinion_hits:
            confidence = min(1.0, fact_hits / max(total, 1))
            return ("fact", round(confidence, 2))
        else:
            confidence = min(1.0, opinion_hits / max(total, 1))
            return ("opinion", round(confidence, 2))

    async def _refine_claim_with_llm(
        self, content: str, title: str
    ) -> tuple[str, float] | None:
        """Pede ao LLM para classificar o tipo de afirmação de um conteúdo ambíguo.

        Só é chamado quando a heurística (`_classify_claim`) não teve confiança
        suficiente. Retorna ``None`` (mantendo o valor heurístico) em caso de
        qualquer falha — nunca derruba o scoring por causa de uma chamada LLM.
        """
        prompt = (
            "Classifique o tipo de afirmação predominante no conteúdo abaixo.\n\n"
            f"Título: {title}\n"
            f"Conteúdo: {content[:800]}\n\n"
            "Tipos possíveis:\n"
            "- fact: afirmação verificável, com fonte, data ou referência objetiva\n"
            "- statistics: contém números, percentuais ou dados estatísticos\n"
            "- opinion: opinião pessoal, especulação ou julgamento subjetivo\n"
            "- unknown: não é possível classificar com confiança\n"
        )
        schema = {
            "type": "object",
            "properties": {
                "claim_type": {
                    "type": "string",
                    "enum": ["fact", "statistics", "opinion", "unknown"],
                },
                "confidence": {"type": "number"},
            },
            "required": ["claim_type", "confidence"],
        }
        try:
            result = await self.llm.generate_structured(prompt, schema)
            claim_type = result.get("claim_type", "unknown")
            if claim_type not in ("fact", "statistics", "opinion", "unknown"):
                claim_type = "unknown"
            confidence = float(result.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            return claim_type, round(confidence, 2)
        except Exception as e:
            logger.warning(
                f"Refinamento de claim_type via LLM falhou, mantendo heuristica: {e}"
            )
            return None

    def _calculate_freshness(self, content: str) -> tuple[float, int | None]:
        years_found = [int(y) for y in _YEAR_PATTERN.findall(content)]

        if not years_found:
            return (0.7, None)

        most_recent = max(years_found)
        age = self.current_year - most_recent

        if age <= 0:
            return (1.0, most_recent)
        elif age == 1:
            return (0.90, most_recent)
        elif age == 2:
            return (0.75, most_recent)
        elif age <= _FRESHNESS_PENALTY_YEARS:
            return (0.60, most_recent)
        elif age <= 5:
            return (0.40, most_recent)
        elif age <= 8:
            return (0.25, most_recent)
        else:
            return (0.10, most_recent)

    def _detect_link_circularity(
        self, results: list[SearchResult]
    ) -> dict[str, list[str]]:
        _url_re = re.compile(r"https?://[^\s\"'<>]+")
        citation_graph: dict[str, set] = {}

        all_result_urls = {r.url for r in results if r.url}

        for result in results:
            if not result.url:
                continue
            content = result.description or ""
            raw_cited = _url_re.findall(content)

            cited = set()
            for url in raw_cited:
                cleaned_url = url.rstrip(".,;:!?()[]{}")
                cited.add(cleaned_url)

            cited_internal = (cited & all_result_urls) - {result.url}
            citation_graph[result.url] = cited_internal

        circular: dict[str, list[str]] = {}

        urls = list(citation_graph.keys())
        for i, url_a in enumerate(urls):
            for url_b in urls[i + 1 :]:
                a_cites_b = url_b in citation_graph.get(url_a, set())
                b_cites_a = url_a in citation_graph.get(url_b, set())
                if a_cites_b and b_cites_a:
                    circular.setdefault(url_a, []).append(url_b)
                    circular.setdefault(url_b, []).append(url_a)

        return circular
