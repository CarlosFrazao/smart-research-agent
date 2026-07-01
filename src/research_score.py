from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import re

from src.types import RankedResult, SearchResult, ResearchMetadata, GapAnalysis


@dataclass
class ResearchScore:
    coverage: float                   # 0.0-1.0: percentual de aspectos cobertos
    diversity: float                  # 0.0-1.0: percentual de diversidade de fontes
    quality: float                    # 0.0-1.0: média ponderada/simples de confidence_score
    reliability: float                # 0.0-1.0: percentual de resultados confiáveis (verified/cited)
    recency: float                    # 0.0-1.0: percentual de resultados recentes
    conflicts: int                    # contagem absoluta de conflitos/contradições
    gaps: int                         # contagem absoluta de lacunas/gaps
    overall: float                    # score final ponderado (0.0-1.0)
    grade: str                        # "A+" | "A" | "B" | "C" | "D" | "F"
    total_sources_used: int
    total_results_analyzed: int
    total_claims_verified: int
    total_claims_unverified: int


class ResearchScoreAggregator:
    WEIGHTS = {
        "coverage": 0.20,
        "diversity": 0.15,
        "quality": 0.25,
        "reliability": 0.20,
        "recency": 0.10,
        "conflict_penalty": 0.10
    }

    def calculate(
        self,
        results: List[RankedResult],
        metadata: ResearchMetadata,
        all_raw_results: List[SearchResult],
        gap_analysis: Optional[GapAnalysis] = None,
        planned_sources: Optional[List[str]] = None,
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
                total_claims_unverified=0
            )

        # 1. Cobertura (Coverage)
        if gap_analysis:
            if gap_analysis.is_complete:
                coverage = 1.0
            else:
                missing = len(gap_analysis.missing_aspects) if gap_analysis.missing_aspects else 0
                coverage = max(0.0, 1.0 - missing * 0.15)
        else:
            coverage = 1.0

        # 2. Diversidade (Diversity)
        sources_used = set()
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

        # 3. Qualidade (Quality)
        quality = sum(
            getattr(r, "confidence_score", getattr(r, "combined_score", getattr(r, "score", 0.0)))
            for r in results
        ) / len(results)

        # 4. Confiabilidade (Reliability)
        verified_count = sum(
            1 for r in results 
            if getattr(r, "evidence_quality", "unknown") in ["verified", "cited"]
        )
        reliability = verified_count / len(results)

        # 5. Recência (Recency)
        now = datetime.now()
        thirty_days_ago = now - timedelta(days=30)
        
        recent_count = 0
        for r in results:
            fetched = getattr(r, "fetched_at", getattr(r, "last_seen", getattr(r, "first_seen", None)))
            if fetched:
                # Converter para naive se for naive no fetched_at
                if fetched.tzinfo is not None and now.tzinfo is None:
                    fetched = fetched.replace(tzinfo=None)
                if fetched >= thirty_days_ago:
                    recent_count += 1
            else:
                # Se não tem fetched_at, assume que foi pego no momento da pesquisa
                recent_count += 1
                
        recency = recent_count / len(results)

        # 6. Conflitos (Conflicts)
        conflicts = sum(
            len(getattr(r, "contradictions", [])) 
            for r in results 
            if getattr(r, "contradictions", None)
        )
        conflict_penalty = min(1.0, conflicts * 0.10)
        conflict_score = max(0.0, 1.0 - conflict_penalty)

        # 7. Gaps
        gaps = len(gap_analysis.missing_aspects) if gap_analysis and gap_analysis.missing_aspects else 0

        # 8. Overall Score Composto
        overall = (
            (coverage * self.WEIGHTS["coverage"]) +
            (diversity * self.WEIGHTS["diversity"]) +
            (quality * self.WEIGHTS["quality"]) +
            (reliability * self.WEIGHTS["reliability"]) +
            (recency * self.WEIGHTS["recency"]) +
            (conflict_score * self.WEIGHTS["conflict_penalty"])
        )

        grade = self._grade(overall)

        total_results_analyzed = len(all_raw_results) if all_raw_results else len(results)
        total_claims_verified = sum(
            1 for r in results 
            if getattr(r, "evidence_quality", "unknown") == "verified"
        )
        total_claims_unverified = sum(
            1 for r in results 
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
            total_claims_unverified=total_claims_unverified
        )

    def _grade(self, overall: float) -> str:
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
        def make_bar(val: float) -> str:
            filled = int(round(val * 10))
            return "█" * filled + "░" * (10 - filled)

        grade_emojis = {
            "A+": "⭐",
            "A": "⭐",
            "B": "✅",
            "C": "⚠️",
            "D": "◆",
            "F": "❌"
        }
        emoji = grade_emojis.get(score.grade, "")

        evaluation_texts = {
            "A+": "Excelência — pesquisa robusta com cobertura plena e máxima confiabilidade.",
            "A": "Muito forte — excelente qualidade de fontes e poucos gaps identificados.",
            "B": "Boa — pesquisa confiável com algumas lacunas menores de escopo.",
            "C": "Regular — contém gaps importantes ou conflitos significativos que exigem revisão.",
            "D": "Fraca — presença acentuada de contradições ou lacunas de dados.",
            "F": "Insuficiente — re-pesquisa focada é altamente recomendada."
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
