import pytest
from datetime import datetime, timedelta
from src.research_score import ResearchScoreAggregator, ResearchScore
from src.types import RankedResult, SearchResult, ResearchMetadata, GapAnalysis

def test_calculate_empty_results():
    aggregator = ResearchScoreAggregator()
    metadata = ResearchMetadata(
        query="test", domain="general", sources=[], total_results=0,
        iterations=1, timestamp=datetime.now(), duration_seconds=1.0
    )
    score = aggregator.calculate(results=[], metadata=metadata, all_raw_results=[])
    
    assert score.overall == 0.0
    assert score.grade == "F"
    assert score.total_sources_used == 0

def test_calculate_mixed_results():
    aggregator = ResearchScoreAggregator()
    metadata = ResearchMetadata(
        query="test", domain="general", sources=["google", "github"], total_results=2,
        iterations=1, timestamp=datetime.now(), duration_seconds=1.0
    )
    
    res1 = RankedResult(
        source="google", title="Google Doc", url="https://google.com", description="test description",
        confidence_score=0.90, evidence_quality="verified", fetched_at=datetime.now()
    )
    res2 = RankedResult(
        source="github", title="Github Repo", url="https://github.com", description="test description 2",
        confidence_score=0.70, evidence_quality="cited", fetched_at=datetime.now()
    )
    
    score = aggregator.calculate(
        results=[res1, res2],
        metadata=metadata,
        all_raw_results=[res1, res2],
        planned_sources=["google", "github"]
    )
    
    assert score.quality == 0.80  # (0.90 + 0.70) / 2
    assert score.reliability == 1.0  # ambos são verified/cited
    assert score.diversity == 1.0  # usou ambos os planejados
    assert score.coverage == 1.0  # sem gap_analysis
    assert score.recency == 1.0
    assert score.overall > 0.70
    assert score.grade in ["A", "B", "A+"]

def test_inject_into_report():
    aggregator = ResearchScoreAggregator()
    score = ResearchScore(
        coverage=0.90, diversity=0.80, quality=0.85, reliability=0.95, recency=0.70,
        conflicts=0, gaps=0, overall=0.86, grade="B", total_sources_used=3,
        total_results_analyzed=5, total_claims_verified=4, total_claims_unverified=1
    )
    
    report_text = "# Relatório de Teste\nConteúdo principal do relatório.\n---\nRodapé do relatório."
    injected = aggregator.inject_into_report(report_text, score)
    
    assert "## 📊 Research Score" in injected
    assert "Métrica" in injected
    assert "Rodapé do relatório." in injected
    assert injected.count("\n---\n") >= 2

def test_grade_thresholds():
    aggregator = ResearchScoreAggregator()
    assert aggregator._grade(0.96) == "A+"
    assert aggregator._grade(0.91) == "A"
    assert aggregator._grade(0.85) == "B"
    assert aggregator._grade(0.75) == "C"
    assert aggregator._grade(0.65) == "D"
    assert aggregator._grade(0.55) == "F"
