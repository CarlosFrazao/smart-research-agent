"""Stage independente para confidence scoring com anti-hallucination e cross-validation.

Este stage desacopla o scoring de confiança do Orchestrator, permitindo:
  1. Skip inteligente para dados estruturados (APIs, JSON, CSV, arXiv, PubMed).
  2. Anti-hallucination checks robustos (factualidade, âncoras, consistência interna).
  3. Cross-validation entre fontes (contradições, circularidade, convergência).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.clients.llm_client import LLMClient

from src.types import RankedResult
from src.pipeline.pipeline import PipelineStage, PipelineContext

logger = logging.getLogger("pipeline.score_stage")

# ── Constantes de Domínio ───────────────────────────────────────────────────

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
        "pubmed.ncbi.nlm.nih.gov",
        "doi.org",
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

# Heurísticas de conteúdo
_CLICKBAIT_RE = re.compile(
    r"\b(you won\'t believe|shocking|secret|hack|trick|amazing|"
    r"incredible|unbelievable|mind.blowing|click here|must.?see)\b",
    re.IGNORECASE,
)

_ABSOLUTE_CLAIM_RE = re.compile(
    r"\b(único|definitivo|perfeito|exclusivo|impossível|sempre|nunca|"
    r"todo mundo|ninguém|100% garantido|com certeza absoluta)\b",
    re.IGNORECASE,
)

_FACT_SIGNALS_RE = re.compile(
    r"\b(?:according\s+to|estudos\s+mostram|pesquisa\s+indica|dados\s+de|"
    r"statistics\s+show|survey\s+found|report\s+says|published\s+in|"
    r"doi:|pmid:|arxiv:|fonte|referência|in\s+\d{4}|em\s+\d{4})\b|"
    r"\b\d+(?:[\.,]\d+)?\s*%|"
    r"\b\d+\s+(?:million|billion|thousand|milhões|bilhões|mil)\b",
    re.IGNORECASE,
)

_OPINION_SIGNALS_RE = re.compile(
    r"\b(?:I\s+think|I\s+believe|in\s+my\s+opinion|acredito|acho\s+que|"
    r"na\s+minha\s+opinião|arguably|seems\s+to|appears\s+to|might\s+be|"
    r"could\s+be|many\s+people\s+think|some\s+argue|critics\s+say)\b",
    re.IGNORECASE,
)

_STATISTICS_RE = re.compile(
    r"\b\d+(?:[\.,]\d+)?\s*%|"
    r"\b\d+\s*(?:percent|porcento)\b|"
    r"\b\d+\s*(?:users|utilizadores|respondents|entrevistados|samples|amostras)\b|"
    r"\b(?:median|average|mean|média|mediana|desvio\s+padrão|correlation|"
    r"correlação|p-value|statistical|significante)\b",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_URL_RE = re.compile(r"https?://[^\s\"\'<>]+")

_POSITIVE_SENTIMENT_RE = re.compile(
    r"\b(fast|better|best|recommended|popular|reliable|stable|"
    r"rápido|melhor|recomendado|popular|confiável|estável|superior)\b",
    re.IGNORECASE,
)

_NEGATIVE_SENTIMENT_RE = re.compile(
    r"\b(slow|worse|worst|avoid|broken|deprecated|buggy|unstable|"
    r"lento|pior|evitar|quebrado|descontinuado|problemático|inferior)\b",
    re.IGNORECASE,
)

_REPETITION_THRESHOLD = 0.30
_FRESHNESS_PENALTY_YEARS = 3


# ── Tipos de Dados Estruturados ─────────────────────────────────────────────

STRUCTURED_SOURCES = frozenset(
    {
        "arxiv",
        "pubmed",
        "semantic_scholar",
        "github_api",
        "pypi",
        "npm",
        "crate",
        "go_pkg",
        "docker_hub",
        "stackoverflow_api",
    }
)

STRUCTURED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "text/csv",
        "application/xml",
        "text/tab-separated-values",
    }
)


# ── Configuração do Stage ───────────────────────────────────────────────────


@dataclass
class ScoreStageConfig:
    """Configuração fina do ScoreStage."""

    confidence_threshold: float = 0.35
    skip_structured: bool = True
    structured_min_score: float = 0.85
    cross_validate: bool = True
    detect_circularity: bool = True
    detect_hallucination: bool = True
    freshness_penalty_enabled: bool = True
    llm_fact_check: bool = False  # Se True, usa LLM para factualidade pesada
    max_repetition_ratio: float = 0.30
    domain_bonus: float = 0.20
    domain_penalty: float = 0.20


# ── ScoreStage ──────────────────────────────────────────────────────────────


class ScoreStage(PipelineStage):
    """Stage independente de confidence scoring.

    Responsabilidades únicas:
      1. Atribuir `confidence_score` (0.0–1.0) a cada resultado.
      2. Preencher `hallucination_flags` e `evidence_quality`.
      3. Cross-validar resultados entre si (contradições, circularidade).
      4. Respeitar skip para dados estruturados.
    """

    name = "confidence_scoring"

    def __init__(
        self,
        config: ScoreStageConfig | None = None,
        llm_client: LLMClient | None = None,
    ):
        self.cfg = config or ScoreStageConfig()
        self.llm = llm_client

    @property
    def current_year(self) -> int:
        return datetime.now(UTC).year

    @property
    def _current_year(self) -> int:
        return self.current_year

    # ── Interface do Pipeline ───────────────────────────────────────────────

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa o stage no contexto de pesquisa do pipeline runner."""
        scored = await self.execute(
            context.ranked,
            context={"query": context.query, "intent": context.intent},
        )
        context.ranked = scored
        return context

    # ── Interface Pública ───────────────────────────────────────────────────

    async def execute(
        self,
        results: list[RankedResult],
        context: dict[str, Any] | None = None,
    ) -> list[RankedResult]:
        """Executa o pipeline de scoring completo.

        Args:
            results: Resultados ranqueados vindos do `QualityRanker` ou buscadores.
            context: Contexto opcional (ex: query original, intent, source_plan).

        Returns:
            Lista de resultados com `confidence_score`, `hallucination_flags`,
            `evidence_quality`, `contradictions` e métricas de cross-validação.
        """
        if not results:
            return []

        # 1. Scoring individual (paralelizado)
        scored = await asyncio.gather(
            *[self._score_single(r, context) for r in results]
        )

        # 2. Cross-validation inter-fontes (se habilitado e houver múltiplos resultados)
        if self.cfg.cross_validate and len(scored) > 1:
            scored = self._cross_validate_batch(scored)

        # 3. Reordenar por confiança descendente (preservando score do ranker como tie-break)
        scored.sort(
            key=lambda r: (r.confidence_score, getattr(r, "score", 0.0)),
            reverse=True,
        )

        logger.info(
            f"ScoreStage: {len(scored)} resultados processados "
            f"(threshold={self.cfg.confidence_threshold})"
        )
        return scored

    # ── Scoring Individual ──────────────────────────────────────────────────

    async def _score_single(
        self,
        result: RankedResult,
        context: dict[str, Any] | None,
    ) -> RankedResult:
        """Score individual com skip para dados estruturados."""
        if self._is_structured(result):
            return self._apply_structured_score(result)

        score = 0.5
        flags: list[str] = []
        content = result.description or ""
        title = result.title or ""
        url = result.url or ""
        word_count = len(content.split())

        # 1. Domínio
        domain = self._extract_domain(url)
        if domain in _TRUSTED_DOMAINS:
            score += self.cfg.domain_bonus
        elif domain in _UNTRUSTED_DOMAINS:
            score -= self.cfg.domain_penalty
            flags.append("untrusted_domain")

        # 2. Volume e densidade de conteúdo
        CODE_SOURCES = {"github", "hackernews", "awesome", "stackoverflow"}
        if word_count >= 300:
            score += 0.15
        elif word_count < 10 and result.source not in CODE_SOURCES:
            score -= 0.30
            flags.append("content_too_short")
        elif word_count < 50 and result.source not in CODE_SOURCES:
            score -= 0.10
            flags.append("content_brief")

        # 3. Métricas de engajamento (fonte-específico)
        if result.source == "github":
            stars = result.metrics.get("stars", 0)
            if stars > 1000:
                score += 0.12
            elif stars > 100:
                score += 0.08
            elif stars > 10:
                score += 0.04

        if result.source == "reddit":
            upvotes = result.metrics.get("upvotes", 0)
            if upvotes > 500:
                score += 0.08
            elif upvotes > 50:
                score += 0.04

        if result.source == "hackernews":
            points = result.metrics.get("points", 0)
            if points > 100:
                score += 0.08
            elif points > 20:
                score += 0.04

        # 4. Ancoragem temporal (indica atualidade e cuidado editorial)
        if _YEAR_RE.search(content):
            score += 0.10

        # 5. Citações externas (indica pesquisa fundamentada)
        cited_urls = _URL_RE.findall(content)
        if len(cited_urls) >= 2:
            score += 0.12
            result.citations = cited_urls[:10]
        elif len(cited_urls) == 1:
            score += 0.06
            result.citations = cited_urls[:10]

        # 6. Título clickbait / sensacionalista
        if not _CLICKBAIT_RE.search(title):
            score += 0.08
        else:
            flags.append("clickbait_title")

        # 7. Score do ranker como sinal adicional
        ranker_score = getattr(result, "score", 0.0) or result.metrics.get(
            "score", result.metrics.get("stars", 0)
        )
        if isinstance(ranker_score, (int, float)) and ranker_score > 70:
            score += 0.08

        # 8. Repetição / baixa entropia lexical (indica spam ou Lorem ipsum)
        if self._has_repetition(content):
            score -= 0.12
            flags.append("repetitive_content")

        # 9. Claims absolutistas (indica viés ou falta de nuance)
        if _ABSOLUTE_CLAIM_RE.search(title):
            score -= 0.15
            flags.append("absolute_claim_detected")

        # 10. Classificação factual (fact vs. opinion vs. statistics)
        claim_type, claim_conf = self._classify_claim(content, title)
        result.metrics["claim_type"] = claim_type
        result.metrics["claim_confidence"] = claim_conf

        if claim_type == "statistics":
            score += 0.12
        elif claim_type == "fact":
            score += 0.08
        elif claim_type == "opinion":
            score -= 0.06
            flags.append("opinion_content")

        # 11. Frescor do conteúdo
        if self.cfg.freshness_penalty_enabled:
            freshness_score, freshness_year = self._calculate_freshness(content)
            result.metrics["freshness_year"] = freshness_year
            result.metrics["freshness_score"] = freshness_score

            if freshness_score < 0.5:
                penalty = round((0.5 - freshness_score) * 0.20, 3)
                score -= penalty
                flags.append("stale_content")

        # 12. LLM Fact-check opcional (pesado, desabilitado por padrão)
        if self.cfg.llm_fact_check and self.llm:
            try:
                llm_verdict = await self._llm_fact_check(result, context)
                if llm_verdict == "unsupported":
                    score -= 0.15
                    flags.append("llm_unsupported_claim")
                elif llm_verdict == "supported":
                    score += 0.05
            except Exception as e:
                logger.debug(f"LLM fact-check falhou para {url[:50]}: {e}")

        # Normalização
        score = max(0.0, min(1.0, score))
        result.confidence_score = round(score, 3)
        result.hallucination_flags = flags
        result.evidence_quality = self._classify_evidence_quality(score)

        return result

    def _apply_structured_score(self, result: RankedResult) -> RankedResult:
        """Aplica scoring mínimo e confiável para dados estruturados.

        Dados de APIs e repositórios estruturados possuem alta confiabilidade
        intrínseca; não faz sentido aplicar heurísticas de NLP.
        """
        result.confidence_score = self.cfg.structured_min_score
        result.hallucination_flags = []
        result.evidence_quality = "verified"
        result.metrics["skipped_scoring"] = True
        result.metrics["claim_type"] = "structured_data"
        logger.debug(
            f"ScoreStage: skip scoring para fonte estruturada '{result.source}'"
        )
        return result

    # ── Cross-Validation Inter-Fontes ───────────────────────────────────────

    def _cross_validate_batch(self, results: list[RankedResult]) -> list[RankedResult]:
        """Executa validação cruzada entre todas as fontes do batch.

        Detecta:
          - Contradições diretas (sentimento oposto sobre mesma entidade).
          - Circularidade de citações (A cita B e B cita A).
          - Convergência factual baixa (claims isolados sem corroboração).
        """
        # Contradições
        contradictions = self._detect_contradictions(results)
        for r in results:
            if r.url in contradictions:
                r.contradictions = contradictions[r.url]
                if "contradicted_by_other_sources" not in r.hallucination_flags:
                    r.hallucination_flags.append("contradicted_by_other_sources")
                r.confidence_score = round(max(0.0, r.confidence_score - 0.10), 3)

        # Circularidade
        if self.cfg.detect_circularity:
            circular = self._detect_link_circularity(results)
            for r in results:
                if r.url in circular:
                    partners = circular[r.url]
                    r.metrics["circular_sources"] = partners
                    if "circular_reference" not in r.hallucination_flags:
                        r.hallucination_flags.append("circular_reference")
                    r.confidence_score = round(max(0.0, r.confidence_score - 0.07), 3)

        # Convergência factual: claims sem corroboração de outra fonte
        self._detect_isolated_claims(results)

        return results

    def _detect_contradictions(
        self, results: list[RankedResult]
    ) -> dict[str, list[str]]:
        """Detecta contradições de sentimento entre resultados sobre tópicos similares."""
        contradictions: dict[str, list[str]] = {}

        for i, r1 in enumerate(results):
            for j, r2 in enumerate(results):
                if i >= j or r1.url == r2.url:
                    continue

                # Overlap de entidade/tópico por palavras-chave
                k1 = set(re.findall(r"\b\w{4,}\b", (r1.title or "").lower()))
                k2 = set(re.findall(r"\b\w{4,}\b", (r2.title or "").lower()))
                if not (k1 & k2):
                    continue

                pos1 = bool(_POSITIVE_SENTIMENT_RE.search(r1.description or ""))
                neg1 = bool(_NEGATIVE_SENTIMENT_RE.search(r1.description or ""))
                pos2 = bool(_POSITIVE_SENTIMENT_RE.search(r2.description or ""))
                neg2 = bool(_NEGATIVE_SENTIMENT_RE.search(r2.description or ""))

                if (pos1 and neg2) or (neg1 and pos2):
                    contradictions.setdefault(r1.url, []).append(r2.url)
                    contradictions.setdefault(r2.url, []).append(r1.url)

        return contradictions

    def _detect_link_circularity(
        self, results: list[RankedResult]
    ) -> dict[str, list[str]]:
        """Detecta circularidade: A cita B e B cita A no mesmo batch."""
        citation_graph: dict[str, set[str]] = {}
        all_urls = {r.url for r in results if r.url}

        for r in results:
            if not r.url:
                continue
            raw = _URL_RE.findall(r.description or "")
            cited = {u.rstrip(".,;:!?()[]{}") for u in raw}
            citation_graph[r.url] = cited & all_urls - {r.url}

        circular: dict[str, list[str]] = {}
        urls = list(citation_graph.keys())
        for i, a in enumerate(urls):
            for b in urls[i + 1 :]:
                if b in citation_graph.get(a, set()) and a in citation_graph.get(
                    b, set()
                ):
                    circular.setdefault(a, []).append(b)
                    circular.setdefault(b, []).append(a)

        return circular

    def _detect_isolated_claims(self, results: list[RankedResult]) -> None:
        """Marca resultados cujos claims parecem isolados (sem corroboração textual)."""
        if len(results) < 3:
            return

        # Heurística simples: verifica se números/estatísticas do resultado aparecem em outros
        for r in results:
            content = r.description or ""
            stats = _STATISTICS_RE.findall(content)
            if not stats:
                continue

            corroboration = 0
            for other in results:
                if other.url == r.url:
                    continue
                other_content = other.description or ""
                for stat in stats[:3]:  # top 3 estatísticas
                    if stat in other_content:
                        corroboration += 1
                        break

            if corroboration == 0 and len(results) > 3:
                if "isolated_claim" not in r.hallucination_flags:
                    r.hallucination_flags.append("isolated_claim")
                r.confidence_score = round(max(0.0, r.confidence_score - 0.05), 3)

    # ── Heurísticas Auxiliares ──────────────────────────────────────────────

    def _is_structured(self, result: RankedResult) -> bool:
        """Determina se o resultado deve pular scoring heurístico."""
        if not self.cfg.skip_structured:
            return False

        if result.source in STRUCTURED_SOURCES:
            return True

        content_type = result.metrics.get("content_type", "")
        if content_type in STRUCTURED_CONTENT_TYPES:
            return True

        # Heurística: resposta de API com campos bem definidos
        if result.metrics.get("is_api_response") is True:
            return True

        return False

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
        return unique_ratio < (1.0 - self.cfg.max_repetition_ratio)

    def _classify_claim(self, content: str, title: str) -> tuple[str, float]:
        text = f"{title} {content}"
        stat_hits = len(_STATISTICS_RE.findall(text))
        fact_hits = len(_FACT_SIGNALS_RE.findall(text))
        opinion_hits = len(_OPINION_SIGNALS_RE.findall(text))
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

    def _calculate_freshness(self, content: str) -> tuple[float, int | None]:
        years = [int(y) for y in _YEAR_RE.findall(content)]
        if not years:
            return (0.7, None)

        most_recent = max(years)
        age = self._current_year - most_recent

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

    def _classify_evidence_quality(self, score: float) -> str:
        if score >= 0.75:
            return "verified"
        elif score >= 0.55:
            return "cited"
        elif score >= 0.35:
            return "inferred"
        return "unknown"

    async def _llm_fact_check(
        self,
        result: RankedResult,
        context: dict[str, Any] | None,
    ) -> str:
        """Consulta LLM para verificar factualidade pesada de um claim."""
        if not self.llm:
            return "unknown"

        prompt = (
            f"Query: {context.get('query', 'N/A') if context else 'N/A'}\\n"
            f"Título: {result.title}\\n"
            f"Conteúdo: {result.description[:800]}\\n\\n"
            "Avalie se as claims principais são factualmente sustentáveis "
            "com base apenas no texto fornecido. Responda apenas: supported, unsupported, unknown."
        )
        response = await self.llm.complete(prompt, max_tokens=10, temperature=0.0)
        verdict = (response or "unknown").strip().lower()
        if verdict not in {"supported", "unsupported", "unknown"}:
            return "unknown"
        return verdict
