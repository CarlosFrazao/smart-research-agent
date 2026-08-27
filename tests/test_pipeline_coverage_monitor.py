"""Testes para a skill pipeline-coverage-monitor."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestPipelineCoverageMonitor:
    """Testes para a função assess_coverage da skill."""

    def test_assess_coverage_complete(self):
        """Quando is_complete=True e gaps cobertos."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": 1,
                    "gap_queries_used": ["query1", "query2"],
                    "gap_sources": {"gap_fill": 1, "graph_explorer": 1, "audit": 0},
                    "is_complete": True,
                    "confidence_score": 0.8,
                }
            ],
            "gap_analysis": MagicMock(
                confidence_score=0.8,
                is_complete=True,
                new_queries=[],
            ),
        }

        result = assess_coverage(context_extra)

        assert result.complete is True
        assert "COVERAGE STATUS" in result.report
        assert "coverage_score" in result.report or "coverage_score" in dir(result)

    def test_assess_coverage_incomplete_max_reached(self):
        """Quando max_iterations atingido mas coverage incompleta."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": i,
                    "gap_queries_used": [f"q{i}"],
                    "gap_sources": {"gap_fill": 1, "graph_explorer": 0, "audit": 0},
                    "is_complete": False,
                }
                for i in range(1, 4)  # 3 iterações
            ],
            "gap_analysis": MagicMock(confidence_score=0.35, is_complete=False),
            "coverage_loop_max_iter_reached": True,
        }

        result = assess_coverage(context_extra)

        assert result.complete is False
        assert "improve_search" in result.message or "improve" in result.message

    def test_assess_coverage_no_history(self):
        """Edge case: nenhum history de cobertura."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        result = assess_coverage({})

        assert result.complete is False
        assert "Nenhum histórico" in result.message

    def test_deduplication_by_query_string(self):
        """Verifica se queries duplicadas são deduplicadas."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": 1,
                    "gap_queries_used": ["query1", "query2"],
                    "gap_sources": {"gap_fill": 2, "graph_explorer": 0, "audit": 0},
                    "is_complete": False,
                },
                {
                    "iteration": 2,
                    "gap_queries_used": ["query2", "query3"],  # query2 duplicated
                    "gap_sources": {"gap_fill": 1, "graph_explorer": 0, "audit": 0},
                    "is_complete": True,
                },
            ],
            "gap_analysis": MagicMock(confidence_score=0.6, is_complete=True),
        }

        result = assess_coverage(context_extra)

        # 3 queries únicos: query1, query2, query3
        assert result.unique_gaps == 3
        assert "3" in str(result.report) or "3" in str(result.unique_gaps)

    def test_backward_compatibility_explicit_scores(self):
        """Testa retrocompatibilidade: gap_confidence_score/gap_is_complete separados."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": 1,
                    "gap_queries_used": ["q1"],
                    "gap_sources": {"gap_fill": 1, "graph_explorer": 0, "audit": 0},
                    "is_complete": False,
                }
            ],
            # Sem gap_analysis, mas com campos separados (backward compat)
            "gap_confidence_score": 0.75,
            "gap_is_complete": True,
        }

        result = assess_coverage(context_extra)

        assert result.complete is True
        assert result.coverage_score > 0

    def test_empty_gap_queries(self):
        """Edge case: history com iteração mas sem gap_queries_used."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": 1,
                    "gap_queries_used": [],
                    "gap_sources": {"gap_fill": 0, "graph_explorer": 0, "audit": 0},
                    "is_complete": True,
                }
            ],
            "gap_analysis": MagicMock(confidence_score=0.9, is_complete=True),
        }

        result = assess_coverage(context_extra)

        assert result.unique_gaps == 0
        assert result.complete is True

    def test_gap_analysis_as_dict(self):
        """Testa que gap_analysis pode ser dict (não só objeto)."""
        from src.skills.pipeline_coverage_monitor import assess_coverage

        context_extra = {
            "coverage_loop_history": [
                {
                    "iteration": 1,
                    "gap_queries_used": ["q1"],
                    "gap_sources": {"gap_fill": 1, "graph_explorer": 0, "audit": 0},
                    "is_complete": True,
                }
            ],
            "gap_analysis": {
                "confidence_score": 0.7,
                "is_complete": True,
                "new_queries": [],
            },
        }

        result = assess_coverage(context_extra)

        assert result.complete is True
        assert result.coverage_score > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
