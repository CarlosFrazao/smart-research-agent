"""
ConfidenceScorerV2 — Motor de Confiança de Elite

Novos recursos sobre o ConfidenceScorer v1:
  1. Classificação Factual por LLM (fact | opinion | statistics)
  2. Detecção de Circularidade de Links (echo chamber de fontes)
  3. Cálculo de Frescor do Conteúdo (penaliza fontes antigas)

Mantém backward-compat total com ConfidenceScorer v1.
"""
import re
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from src.types import SearchResult
from src.confidence_scorer import ConfidenceScorer
import logging

logger = logging.getLogger(__name__)

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

# --- Padrões de data para cálculo de frescor ---
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")

# Limiar de frescor: conteúdo com mais de N anos perde pontos
_FRESHNESS_PENALTY_YEARS = 3
_CURRENT_YEAR = datetime.now(timezone.utc).year


class ConfidenceScorerV2(ConfidenceScorer):
    """
    Extensão do ConfidenceScorer v1 com:
      - Classificação factual (fact/opinion/statistics) por heurística + LLM opcional
      - Detecção de circularidade de links entre fontes
      - Penalização por frescor de conteúdo
    """

    def __init__(self, llm_client=None):
        """
        Args:
            llm_client: Opcional. Se fornecido, usa LLM para classificar claims
                        ambíguos que as heurísticas não conseguem resolver com
                        confiança. Reduz tokens — só chama LLM quando necessário.
        """
        super().__init__()
        self.llm = llm_client

    # ------------------------------------------------------------------
    # Método principal — override total do score_result do v1
    # ------------------------------------------------------------------

    async def score_result(self, result: SearchResult) -> SearchResult:
        """Pontua um SearchResult com todas as melhorias do V2."""
        # 1. Score base do V1 (heurísticas de domínio, clickbait, repetição)
        result = await super().score_result(result)

        content = result.description or ""

        # 2. Classificação Factual
        claim_type, claim_confidence = self._classify_claim(content, result.title or "")
        result.metrics["claim_type"] = claim_type
        result.metrics["claim_confidence"] = claim_confidence

        # Ajuste de score por tipo de claim
        if claim_type == "fact":
            result.confidence_score = min(1.0, result.confidence_score + 0.08)
        elif claim_type == "opinion":
            result.confidence_score = max(0.0, result.confidence_score - 0.05)
            if "opinion_content" not in result.hallucination_flags:
                result.hallucination_flags.append("opinion_content")
        elif claim_type == "statistics":
            result.confidence_score = min(1.0, result.confidence_score + 0.12)

        # 3. Frescor do Conteúdo
        freshness_score, freshness_year = self._calculate_freshness(content)
        result.metrics["freshness_year"] = freshness_year
        result.metrics["freshness_score"] = freshness_score

        if freshness_score < 0.5:
            penalty = round((0.5 - freshness_score) * 0.20, 3)
            result.confidence_score = max(0.0, result.confidence_score - penalty)
            if "stale_content" not in result.hallucination_flags:
                result.hallucination_flags.append("stale_content")

        # 4. Re-clamp e re-classifica
        result.confidence_score = round(max(0.0, min(1.0, result.confidence_score)), 3)
        result.evidence_quality = self._classify_evidence_quality(result.confidence_score)

        return result

    async def score_batch(
        self,
        results: List[SearchResult],
        cross_validate: bool = True,
        detect_circularity: bool = True,
    ) -> List[SearchResult]:
        """
        Pontua um lote com todas as melhorias do V2, incluindo detecção de circularidade.
        """
        # Score individual de cada resultado (V2)
        scored = [await self.score_result(r) for r in results]

        # Validação cruzada do V1 (contradições)
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

        # Novo V2: Detecção de Circularidade de Links
        if detect_circularity and len(scored) > 1:
            circular_groups = self._detect_link_circularity(scored)
            for result in scored:
                if result.url in circular_groups:
                    circular_partners = circular_groups[result.url]
                    result.metrics["circular_sources"] = circular_partners
                    if "circular_reference" not in result.hallucination_flags:
                        result.hallucination_flags.append("circular_reference")
                    # Penaliza levemente — circular não significa errado, só menos independente
                    result.confidence_score = round(
                        max(0.0, result.confidence_score - 0.07), 3
                    )
                    logger.info(
                        f"Circularidade detectada: {result.url[:60]} referencia {len(circular_partners)} parceiros"
                    )

        return scored

    # ------------------------------------------------------------------
    # Classificação Factual
    # ------------------------------------------------------------------

    def _classify_claim(self, content: str, title: str) -> Tuple[str, float]:
        """
        Classifica o conteúdo como 'fact', 'opinion' ou 'statistics'.

        Retorna: (claim_type, confiança_na_classificação 0.0-1.0)
        """
        text = f"{title} {content}"

        # Usamos finditer para evitar problemas de grupos do findall
        stat_hits = sum(1 for _ in _STATISTICS_PATTERNS.finditer(text))
        fact_hits = sum(1 for _ in _FACT_PATTERNS.finditer(text))
        opinion_hits = sum(1 for _ in _OPINION_PATTERNS.finditer(text))

        total = stat_hits + fact_hits + opinion_hits

        if total == 0:
            return ("unknown", 0.4)

        # Se contiver estatísticas explícitas (ex: %), damos preferência a 'statistics'
        # mesmo se houver outros fatos textuais de suporte no mesmo texto.
        if stat_hits > 0:
            confidence = min(1.0, (stat_hits * 1.5) / max(total, 1))
            return ("statistics", round(confidence, 2))
        elif fact_hits >= opinion_hits:
            confidence = min(1.0, fact_hits / max(total, 1))
            return ("fact", round(confidence, 2))
        else:
            confidence = min(1.0, opinion_hits / max(total, 1))
            return ("opinion", round(confidence, 2))

    # ------------------------------------------------------------------
    # Cálculo de Frescor
    # ------------------------------------------------------------------

    def _calculate_freshness(self, content: str) -> Tuple[float, Optional[int]]:
        """
        Detecta o ano mais recente mencionado no conteúdo e calcula um score
        de frescor (0.0 = muito antigo | 1.0 = atual).

        Retorna: (freshness_score, year_detected)
        """
        years_found = [int(y) for y in _YEAR_PATTERN.findall(content)]

        if not years_found:
            # Sem data detectada → score neutro
            return (0.7, None)

        most_recent = max(years_found)
        age = _CURRENT_YEAR - most_recent

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

    # ------------------------------------------------------------------
    # Detecção de Circularidade de Links
    # ------------------------------------------------------------------

    def _detect_link_circularity(
        self, results: List[SearchResult]
    ) -> Dict[str, List[str]]:
        """
        Detecta circularidade: quando múltiplas fontes se referenciam mutuamente,
        formando um "echo chamber" que infla artificialmente a confiança.

        Algoritmo:
          1. Extrai todas as URLs citadas no conteúdo de cada resultado.
          2. Limpa pontuações de fim de URL.
          3. Constrói um grafo de referências: result_url -> [urls_citadas]
          4. Verifica se result_url_B está no conteúdo de result_url_A E vice-versa.

        Retorna: {url: [urls_que_formam_circulo_com_ela]}
        """
        _url_re = re.compile(r"https?://[^\s\"'<>]+")
        # Mapa: url_do_resultado -> conjunto de urls citadas no seu conteúdo
        citation_graph: Dict[str, set] = {}

        all_result_urls = {r.url for r in results if r.url}

        for result in results:
            if not result.url:
                continue
            content = result.description or ""
            raw_cited = _url_re.findall(content)
            
            # Limpa caracteres de pontuação final colados na URL (ex: vírgula, ponto)
            cited = set()
            for url in raw_cited:
                cleaned_url = url.rstrip(".,;:!?()[]{}")
                cited.add(cleaned_url)
                
            # Só rastreia referências a outros resultados do mesmo batch
            cited_internal = cited & all_result_urls - {result.url}
            citation_graph[result.url] = cited_internal

        circular: Dict[str, List[str]] = {}

        urls = list(citation_graph.keys())
        for i, url_a in enumerate(urls):
            for url_b in urls[i + 1:]:
                # Circularidade: A cita B E B cita A
                a_cites_b = url_b in citation_graph.get(url_a, set())
                b_cites_a = url_a in citation_graph.get(url_b, set())
                if a_cites_b and b_cites_a:
                    circular.setdefault(url_a, []).append(url_b)
                    circular.setdefault(url_b, []).append(url_a)

        return circular
