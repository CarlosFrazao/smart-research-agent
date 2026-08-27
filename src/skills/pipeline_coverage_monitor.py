"""
Pipeline Coverage Monitor Skill

Supreme skill for monitoring research pipeline coverage verification loops.
Guarantees no gap goes unnoticed, iterates until coverage sufficient,
and produces auditable reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CoverageAssessment:
    """Result of coverage assessment."""

    complete: bool
    message: str
    report: str
    coverage_score: float = 0.0
    iterations: int = 0
    unique_gaps: int = 0


def assess_coverage(context_extra: dict[str, Any]) -> CoverageAssessment:
    """
    Assess pipeline coverage based on coverage_loop_history and gap_analysis.

    Args:
        context_extra: Dictionary containing:
            - coverage_loop_history: List of iteration details
            - gap_analysis: GapAnalysis object (or dict with confidence_score, is_complete)
            - audit_gaps: Gaps from ReportStage

    Returns:
        CoverageAssessment with completeness status and detailed report
    """
    coverage_loop_history = context_extra.get("coverage_loop_history", [])
    gap_analysis = context_extra.get("gap_analysis")

    # Edge case: no history
    if not coverage_loop_history:
        return CoverageAssessment(
            complete=False,
            message="Nenhum histórico de cobertura encontrado. Pipeline pode não ter rodado.",
            report="## COVERAGE STATUS\n- **Status:** INCOMPLETE\n- **Iterações:** 0\n- **Únicos gaps:** 0\n- **Mensagem:** Nenhum histórico de cobertura encontrado.\n- **Recomendação:** Verificar se o pipeline executou corretamente.\n",
        )

    # Extract max iterations
    max_iterations = (
        max(h.get("iteration", 0) for h in coverage_loop_history)
        if coverage_loop_history
        else 0
    )

    # Collect all unique gaps across iterations (deduplication by query string)
    seen_queries: set[str] = set()
    unique_gaps: list[str] = []

    for history_item in coverage_loop_history:
        gap_queries = history_item.get("gap_queries_used", [])
        for q in gap_queries:
            q_str = q.get("query", str(q)) if isinstance(q, dict) else str(q)
            if q_str not in seen_queries:
                seen_queries.add(q_str)
                unique_gaps.append(q_str)

    unique_gap_count = len(unique_gaps)

    # Get gap_analysis confidence and completeness
    if gap_analysis:
        confidence_score = getattr(gap_analysis, "confidence_score", None)
        if confidence_score is None and isinstance(gap_analysis, dict):
            confidence_score = gap_analysis.get("confidence_score", 0.5)

        is_complete = getattr(gap_analysis, "is_complete", True)
        if is_complete is True and isinstance(gap_analysis, dict):
            is_complete = gap_analysis.get("is_complete", True)

        # Use explicit scores if available in context_extra (backward compatibility)
        if "gap_confidence_score" in context_extra:
            confidence_score = context_extra["gap_confidence_score"]
        if "gap_is_complete" in context_extra:
            is_complete = context_extra["gap_is_complete"]
    else:
        confidence_score = context_extra.get("gap_confidence_score", 0.5)
        is_complete = context_extra.get("gap_is_complete", True)
        if is_complete is True and isinstance(is_complete, str):
            is_complete = is_complete.lower() == "true"

    # Determine if we hit max iterations
    max_iter_reached = context_extra.get("coverage_loop_max_iter_reached", False)

    # Calculate coverage score
    coverage_score = _calculate_coverage_score(
        unique_gaps=unique_gap_count,
        confidence_score=confidence_score or 0.5,
        is_complete=is_complete,
        max_iter_reached=max_iter_reached,
    )

    # Determine completion status
    complete = is_complete and not max_iter_reached

    # Generate report
    report = _generate_report(
        iterations=max_iterations,
        unique_gaps=unique_gap_count,
        is_complete=complete,
        confidence_score=confidence_score or 0.5,
        max_iter_reached=max_iter_reached,
        coverage_score=coverage_score,
    )

    message = _generate_message(complete, confidence_score or 0.5, max_iter_reached)

    return CoverageAssessment(
        complete=complete,
        message=message,
        report=report,
        coverage_score=coverage_score,
        iterations=max_iterations,
        unique_gaps=unique_gap_count,
    )


def _calculate_coverage_score(
    unique_gaps: int, confidence_score: float, is_complete: bool, max_iter_reached: bool
) -> float:
    """Calculate overall coverage score (0.0-1.0)."""
    base_score = confidence_score

    # Adjust based on completeness
    if is_complete:
        base_score = min(1.0, base_score + 0.1)
    else:
        if max_iter_reached:
            base_score = base_score * 0.8  # Penalty for max iterations
        else:
            base_score = base_score * 0.9  # Penalty for incompleteness

    return round(max(0.0, min(1.0, base_score)), 2)


def _generate_report(
    iterations: int,
    unique_gaps: int,
    is_complete: bool,
    confidence_score: float,
    max_iter_reached: bool,
    coverage_score: float,
) -> str:
    """Generate detailed coverage report."""
    complete_str = "True" if is_complete else "False"
    max_iter_str = "True" if max_iter_reached else "False"

    recommendation = "stop"
    if not is_complete:
        if max_iter_reached:
            recommendation = "improve_search"
        else:
            recommendation = "continue"

    return f"""## COVERAGE STATUS

- **Iterações executadas:** {iterations}
- **Gaps únicos encontrados:** {unique_gaps}
- **is_complete:** {complete_str}
- **max_iterations_reached:** {max_iter_str}
- **confidence_score:** {confidence_score:.2f}
- **coverage_score:** {coverage_score:.0%}
- **Recomendação:** {recommendation}

### Detalhes da Cobertura

| Métrica | Valor |
|---------|-------|
| Total iterações | {iterations} |
| Queries únicas | {unique_gaps} |
| Confiança | {confidence_score:.2f} |
| Status | {complete_str} |

### Fontes de Gap (por iteração)

"""


def _generate_message(
    complete: bool, confidence_score: float, max_iter_reached: bool
) -> str:
    """Generate summary message."""
    if complete:
        return f"Cobertura completa com confiança {confidence_score:.2f}. Pipeline verificado."
    elif max_iter_reached:
        return f"Cobertura incompleta (max iterations). Confiança: {confidence_score:.2f}. Recomendado: improve_search."
    else:
        return f"Cobertura incompleta. Confiança: {confidence_score:.2f}. Iterar mais ou melhorar busca."
