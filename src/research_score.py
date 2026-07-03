"""Módulo de calculo e agergação do score de qualidade da pesquisa.

Fornece `ResearchScore` (dataclass) e `ResearchScoreAggregator` para calcular
dimensoes de cobertura, diversidade, qualidade, confiabilidade, recencia e
conflitos, resultando em um score final ponderado e nota de A+ a F.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.types import GapAnalysis, RankedResult, ResearchMetadata, SearchResult


@dataclass
class ResearchScore:
    """Score agregado de qualidade de uma sessao de pesquisa.

    Attributes:
        coverage: Percentual de aspectos da query cobertos (0.0-1.0).
        diversity: Percentual de diversidade de fontes utilizadas (0.0-1.0).
        quality: Media ponderada de confidence_score dos resultados (0.0-1.0).
        reliability: Percentual de resultados verificados ou citados (0.0-1.0).
        recency: Percentual de resultados recentes (< 180 dias) (0.0-1.0).
        conflicts: Numero absoluto de conflitos/contradicoes detectados.
        gaps: Numero absoluto de lacunas de cobertura identificadas.
        overall: Score final ponderado (0.0-1.0).
        grade: Nota final como string (A+, A, B, C, D ou F).
        total_sources_used: Total de fontes distintas usadas na pesquisa.
        total_results_analyzed: Total de resultados brutos analisados.
        total_claims_verified: Total de afirmacoes verificadas por fontes.
        total_claims_unverified: Total de afirmacoes sem verificacao.
    """

    coverage: float  # 0.0-1.0: percentual de aspectos cobertos
    diversity: float  # 0.0-1.0: percentual de diversidade de fontes
    quality: float  # 0.0-1.0: média ponderada/simples de confidence_score
    reliability: float  # 0.0-1.0: percentual de resultados confiáveis (verified/cited)
    recency: float  # 0.0-1.0: percentual de resultados recentes
    conflicts: int  # contagem absoluta de conflitos/contradições
    gaps: int  # contagem absoluta de lacunas/gaps
    overall: float  # score final ponderado (0.0-1.0)
    grade: str  # "A+" | "A" | "B" | "C" | "D" | "F"
    total_sources_used: int
    total_results_analyzed: int
    total_claims_verified: int
    total_claims_unverified: int


class ResearchScoreAggregator:
    """Agregador que calcula o `ResearchScore` de uma sessao de pesquisa.

    Computa dimensoes individualmente (coverage, diversity, quality, reliability,
    recency, conflicts) e combina em um score geral ponderado com nota A+-F.
    """

    WEIGHTS = {
        "coverage": 0.20,
        "diversity": 0.15,
        "quality": 0.25,
        "reliability": 0.20,
        "recency": 0.10,
        "conflict_penalty": 0.10,
    }

    def calculate(
        self,
        results: list[Any],
        metadata: ResearchMetadata,
        all_raw_results: list[SearchResult],
        gap_analysis: GapAnalysis | None = None,
        planned_sources: list[str] | None = None,
        peer_review_report: Any | None = None,
    ) -> ResearchScore:
        """
        Calcula as pontuações e gera o ResearchScore agregado para a pesquisa.
        """
        if not results:
            return ResearchScore(
                coverage=0.0,
                diversity=0.0,
                quality=0.0,
                reliability=0.0,
                recency=0.0,
                conflicts=0,
                gaps=0,
                overall=0.0,
                grade="F",
                total_sources_used=0,
                total_results_analyzed=len(all_raw_results) if all_raw_results else 0,
                total_claims_verified=0,
                total_claims_unverified=0,
            )

        coverage = self._calculate_coverage(gap_analysis)
        diversity, total_sources_used = self._calculate_diversity(
            results, planned_sources
        )
        quality = self._calculate_quality(results)
        reliability = self._calculate_reliability(results)
        recency = self._calculate_recency(results)
        conflicts, conflict_score = self._calculate_conflicts(results)

        # 7. Gaps
        gaps = (
            len(gap_analysis.missing_aspects)
            if gap_analysis and gap_analysis.missing_aspects
            else 0
        )

        # 8. Overall Score Composto
        overall = (
            (coverage * self.WEIGHTS["coverage"])
            + (diversity * self.WEIGHTS["diversity"])
            + (quality * self.WEIGHTS["quality"])
            + (reliability * self.WEIGHTS["reliability"])
            + (recency * self.WEIGHTS["recency"])
            + (conflict_score * self.WEIGHTS["conflict_penalty"])
        )
        overall = min(1.0, max(0.0, overall))

        # Penalidades baseadas no Peer Review
        if peer_review_report is not None:
            critical_count = getattr(peer_review_report, "critical_count", 0)
            major_count = getattr(peer_review_report, "major_count", 0)
            minor_count = getattr(peer_review_report, "minor_count", 0)

            peer_penalty = (
                (critical_count * 0.10) + (major_count * 0.05) + (minor_count * 0.02)
            )
            peer_penalty = min(0.30, peer_penalty)
            overall = max(0.0, overall - peer_penalty)

        grade = self._grade(overall)

        total_results_analyzed = (
            len(all_raw_results) if all_raw_results else len(results)
        )
        total_claims_verified = sum(
            1
            for r in results
            if getattr(r, "evidence_quality", "unknown") == "verified"
        )
        total_claims_unverified = sum(
            1
            for r in results
            if getattr(r, "evidence_quality", "unknown") in ["unverified", "unknown"]
        )

        return ResearchScore(
            coverage=coverage,
            diversity=diversity,
            quality=quality,
            reliability=reliability,
            recency=recency,
            conflicts=conflicts,
            gaps=gaps,
            overall=overall,
            grade=grade,
            total_sources_used=total_sources_used,
            total_results_analyzed=total_results_analyzed,
            total_claims_verified=total_claims_verified,
            total_claims_unverified=total_claims_unverified,
        )

    def _calculate_coverage(self, gap_analysis: GapAnalysis | None) -> float:
        """Calcula o score de cobertura baseado nos gaps identificados.

        Args:
            gap_analysis: Analise de lacunas da pesquisa, ou None se indisponivel.

        Returns:
            float: Score entre 0.0 (sem cobertura) e 1.0 (cobertura total).
        """
        if gap_analysis:
            if gap_analysis.is_complete:
                return 1.0
            else:
                missing = (
                    len(gap_analysis.missing_aspects)
                    if gap_analysis.missing_aspects
                    else 0
                )
                return max(0.0, 1.0 - missing * 0.15)
        return 1.0

    def _calculate_diversity(
        self, results: list[RankedResult], planned_sources: list[str] | None
    ) -> tuple[float, int]:
        """Calcula a diversidade de fontes dos resultados em relacao ao plano.

        Args:
            results: Lista de resultados ranqueados.
            planned_sources: Lista de fontes planejadas pelo `SourcePlanner`.

        Returns:
            tuple[float, int]: Score de diversidade (0.0-1.0) e numero de fontes distintas.
        """
        sources_used: set[str] = set()
        for r in results:
            r_sources = getattr(r, "sources", None)
            if isinstance(r_sources, list):
                sources_used.update(src for src in r_sources if src)
            else:
                r_source = getattr(r, "source", None)
                if r_source:
                    sources_used.add(r_source)
        total_sources_used = len(sources_used)
        if planned_sources:
            unique_planned = set(planned_sources)
            if unique_planned:
                diversity = min(1.0, total_sources_used / len(unique_planned))
            else:
                diversity = min(1.0, total_sources_used / 5.0)
        else:
            diversity = min(1.0, total_sources_used / 5.0)
        return diversity, total_sources_used

    def _calculate_quality(self, results: list[RankedResult]) -> float:
        """Calcula a qualidade media dos resultados via confidence_score.

        Args:
            results: Lista de resultados ranqueados com atributo ``confidence_score``.

        Returns:
            float: Media do confidence_score normalizada para 0.0-1.0.
        """
        raw_scores = [
            getattr(
                r,
                "confidence_score",
                getattr(r, "combined_score", getattr(r, "score", 0.0)),
            )
            for r in results
        ]
        if not raw_scores:
            return 0.0

        avg = sum(raw_scores) / len(raw_scores)
        # Se avg > 1.0, indica escala de pontuação 0-100; divide por 100 para normalizar.
        if avg > 1.0:
            avg = avg / 100.0

        return min(1.0, max(0.0, avg))

    def _calculate_reliability(self, results: list[RankedResult]) -> float:
        """Calcula o percentual ponderado de resultados classificados como verified, cited ou inferred.

        Args:
            results: Lista de resultados ranqueados com atributo ``evidence_quality``.

        Returns:
            float: Percentual de resultados confiaveis (0.0-1.0).
        """
        reliable_score = 0.0
        for r in results:
            q = getattr(r, "evidence_quality", "unknown")
            if q in ["verified", "cited"]:
                reliable_score += 1.0
            elif q == "inferred":
                reliable_score += 0.5
        return reliable_score / len(results)

    def _calculate_recency(self, results: list[RankedResult]) -> float:
        """Calcula o percentual de resultados com data recente (< 180 dias).

        Args:
            results: Lista de resultados ranqueados com atributo ``fetched_at``.

        Returns:
            float: Percentual de resultados recentes (0.0-1.0).
        """
        now = datetime.now()
        thirty_days_ago = now - timedelta(days=30)

        recent_count = 0
        for r in results:
            fetched = getattr(
                r, "fetched_at", getattr(r, "last_seen", getattr(r, "first_seen", None))
            )
            if fetched:
                if fetched.tzinfo is not None and now.tzinfo is None:
                    fetched = fetched.replace(tzinfo=None)
                if fetched >= thirty_days_ago:
                    recent_count += 1
            else:
                recent_count += 1

        return recent_count / len(results)

    def _calculate_conflicts(self, results: list[RankedResult]) -> tuple[int, float]:
        """Detecta conflitos/contradicoes entre os resultados (mesma entidade, scores divergentes).

        Args:
            results: Lista de resultados ranqueados.

        Returns:
            tuple[int, float]: Numero de conflitos e score de penalizacao (0.0-1.0).
                Um score mais proximo de 0.0 indica mais conflitos.
        """
        conflicts = sum(
            len(getattr(r, "contradictions", []))
            for r in results
            if getattr(r, "contradictions", None)
        )
        conflict_penalty = min(1.0, conflicts * 0.10)
        conflict_score = max(0.0, 1.0 - conflict_penalty)
        return conflicts, conflict_score

    def _grade(self, overall: float) -> str:
        """Converte um score numerico (0.0-1.0) em nota de A+ a F.

        Args:
            overall: Score geral ponderado entre 0.0 e 1.0.

        Returns:
            str: Nota qualitativa (A+, A, B, C, D ou F).
        """
        if overall >= 0.95:
            return "A+"
        elif overall >= 0.90:
            return "A"
        elif overall >= 0.80:
            return "B"
        elif overall >= 0.70:
            return "C"
        elif overall >= 0.60:
            return "D"
        else:
            return "F"

    def _format_score_block(self, score: ResearchScore) -> str:
        """Formata o `ResearchScore` como bloco Markdown com barras visuais.

        Args:
            score: Score agregado calculado pelo metodo ``calculate``.

        Returns:
            str: Bloco Markdown com barras de progresso ASCII e metricas.
        """

        def make_bar(val: float) -> str:
            safe_val = min(1.0, max(0.0, val))
            filled = int(round(safe_val * 10))
            return "█" * filled + "░" * (10 - filled)

        grade_emojis = {"A+": "⭐", "A": "⭐", "B": "✅", "C": "⚠️", "D": "◆", "F": "❌"}
        emoji = grade_emojis.get(score.grade, "")

        evaluation_texts = {
            "A+": "Excelência — pesquisa robusta com cobertura plena e máxima confiabilidade.",
            "A": "Muito forte — excelente qualidade de fontes e poucos gaps identificados.",
            "B": "Boa — pesquisa confiável com algumas lacunas menores de escopo.",
            "C": "Regular — contém gaps importantes ou conflitos significativos que exigem revisão.",
            "D": "Fraca — presença acentuada de contradições ou lacunas de dados.",
            "F": "Insuficiente — re-pesquisa focada é altamente recomendada.",
        }
        eval_text = evaluation_texts.get(score.grade, "")

        block = f"""
## 📊 Research Score: **{score.grade}** {emoji}

| Métrica | Valor | Barra |
| :--- | :--- | :--- |
| Cobertura | {score.coverage:.0%} | {make_bar(score.coverage)} |
| Diversidade | {score.diversity:.0%} | {make_bar(score.diversity)} |
| Qualidade | {score.quality:.0%} | {make_bar(score.quality)} |
| Confiabilidade | {score.reliability:.0%} | {make_bar(score.reliability)} |
| Recência | {score.recency:.0%} | {make_bar(score.recency)} |
| **Overall** | **{score.overall:.1%}** | **{make_bar(score.overall)}** |

**Detalhes:** {score.total_results_analyzed} fontes analisadas | {score.total_claims_verified} claims verificados | {score.gaps} gaps detectados | {score.conflicts} conflito(s) de evidência.

**Avaliação:** {eval_text}
"""
        return block.strip()

    def inject_into_report(self, report: str, score: ResearchScore) -> str:
        """Injeta o bloco de score no relatorio Markdown gerado.

        Insere o bloco formatado logo antes da secao ``## 2.`` (projetos/ferramentas).
        Se o marcador nao for encontrado, apende ao final do relatorio.

        Args:
            report: Relatorio Markdown completo gerado pelo `ReportGenerator`.
            score: `ResearchScore` calculado pelo agregador.

        Returns:
            str: Relatorio com o bloco de score injetado.
        """
        score_md = self._format_score_block(score)

        # Procura a última linha de divisão "---" para injetar o bloco antes dela (rodapé)
        parts = report.split("\n---\n")
        if len(parts) > 1:
            # Reinsere antes da última parte
            footer = parts[-1]
            body = "\n---\n".join(parts[:-1])
            return f"{body}\n\n---\n\n{score_md}\n\n---\n\n{footer}"
        else:
            return f"{report}\n\n---\n\n{score_md}"
