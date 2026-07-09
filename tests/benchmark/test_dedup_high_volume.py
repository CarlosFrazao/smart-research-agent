import pytest
import time


@pytest.mark.benchmark
def test_conflict_detection_with_20_sources():
    """Garante que conflict_detector não regride em performance com 20+ fontes."""
    from src.conflict_detector import ConflictDetector

    # Simular 200 resultados de 20 fontes diferentes
    results = [
        {"url": f"https://source{i % 20}.com/result{j}", "title": f"Result {j}", "score": 0.7}
        for i in range(20) for j in range(10)
    ]

    detector = ConflictDetector()
    start = time.monotonic()
    deduped = detector.deduplicate(results)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"Deduplicação lenta: {elapsed:.2f}s para 200 resultados"
    assert len(deduped) <= len(results)


@pytest.mark.benchmark
def test_search_stage_timeout_per_source():
    """Verifica que timeouts são diferenciados por categoria de fonte."""
    from src.pipeline.stages.search_stage import get_timeout_for_source, UNTRUSTED_SOURCES

    assert get_timeout_for_source("github") < get_timeout_for_source("firecrawl")
    assert get_timeout_for_source("wikipedia") <= 10.0
    assert get_timeout_for_source("quora") >= 15.0
