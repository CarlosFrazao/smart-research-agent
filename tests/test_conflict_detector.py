import pytest
from src.conflict_detector import ConflictDetector, NumericClaim, ConflictReport, Conflict
from src.types import SearchResult

def test_detect_empty_or_single():
    detector = ConflictDetector()
    report = detector.detect([])
    assert report.conflict_count == 0
    assert report.total_claims_extracted == 0
    
    r = SearchResult(
        source="google", title="Single result", url="http://test.com/1",
        description="O mercado de IA cresceu 18% em 2025."
    )
    report2 = detector.detect([r])
    assert report2.conflict_count == 0
    assert report2.total_claims_extracted > 0

def test_detect_divergent_claims():
    detector = ConflictDetector(divergence_threshold=0.20)
    
    r1 = SearchResult(
        source="sourceA", title="Report A", url="http://test.com/a",
        description="O mercado de IA cresceu 18% em 2025."
    )
    r2 = SearchResult(
        source="sourceB", title="Report B", url="http://test.com/b",
        description="O mercado de IA cresceu 27% em 2025."
    )
    
    report = detector.detect([r1, r2])
    assert report.conflict_count == 1
    conflict = report.conflicts[0]
    assert conflict.severity in ["high", "critical"]
    assert conflict.divergence_ratio == 0.50  # (27 - 18) / 18 = 0.50
    assert conflict.metric_name == "mercado ia cresceu"
    assert conflict.claims[0].unit == "%"

def test_detect_close_claims_below_threshold():
    detector = ConflictDetector(divergence_threshold=0.50)
    
    r1 = SearchResult(
        source="sourceA", title="Report A", url="http://test.com/a",
        description="O mercado de IA cresceu 18% em 2025."
    )
    r2 = SearchResult(
        source="sourceB", title="Report B", url="http://test.com/b",
        description="O mercado de IA cresceu 20% em 2025."
    )
    
    report = detector.detect([r1, r2])
    # Divergência = (20 - 18) / 18 = 11.1% (abaixo do threshold de 50%)
    assert report.conflict_count == 0

def test_format_conflicts_for_report():
    detector = ConflictDetector()
    r1 = SearchResult(
        source="sourceA", title="Report A", url="http://test.com/a",
        description="O mercado de IA cresceu 18% em 2025."
    )
    r2 = SearchResult(
        source="sourceB", title="Report B", url="http://test.com/b",
        description="O mercado de IA cresceu 40% em 2025."
    )
    
    report = detector.detect([r1, r2])
    formatted = detector.format_conflicts_for_report(report)
    
    assert "## ⚠️ Conflitos Detectados nas Fontes" in formatted
    assert "mercado ia cresceu" in formatted
    assert "18%" in formatted
    assert "40%" in formatted
    assert "122%" in formatted  # (40 - 18)/18 = 1.22
