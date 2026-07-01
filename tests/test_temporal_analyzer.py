import pytest
from datetime import datetime
from src.temporal_analyzer import TemporalAnalyzer
from src.types import SynthesizedResult


@pytest.fixture
def analyzer():
    return TemporalAnalyzer()


def test_extract_timeline_various_formats(analyzer):
    """Verifica se o extrator identifica múltiplos formatos de datas no texto."""
    results = [
        SynthesizedResult(
            entity="Proj A",
            title="Projeto Alpha",
            description="Lançado em 2023-05-15 como beta.",
            sources=["github"],
            urls=["http://example.com"],
            combined_score=80.0,
            metrics={"updated_at": datetime(2025, 2, 10)},
            highlights=["Destaque em junho de 2024."],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
        ),
        SynthesizedResult(
            entity="Proj B",
            title="Projeto Beta",
            description="Criado em 12/2022 e atualizado em 2026.",
            sources=["github"],
            urls=["http://example.com"],
            combined_score=90.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
        )
    ]

    timeline = analyzer.extract_timeline(results)
    
    # Deve encontrar:
    # 1. 2023-05-15 (de Proj A)
    # 2. 2025-02-10 (de updated_at Proj A)
    # 3. junho de 2024 -> 2024-06-01 (de Proj A)
    # 4. 12/2022 -> 2022-12-01 (de Proj B)
    # 5. 2026 -> 2026-01-01 (de Proj B)

    # Ordenado cronologicamente, a ordem deve ser:
    # 2022-12-01 (Proj B)
    # 2023-05-15 (Proj A)
    # 2024-06-01 (Proj A)
    # 2025-02-10 (Proj A)
    # 2026-01-01 (Proj B)

    dates = [t[0].date() for t in timeline]
    assert datetime(2022, 12, 1).date() in dates
    assert datetime(2023, 5, 15).date() in dates
    assert datetime(2024, 6, 1).date() in dates
    assert datetime(2025, 2, 10).date() in dates
    assert datetime(2026, 1, 1).date() in dates

    assert len(timeline) == 5
    assert timeline[0][0].year == 2022
    assert timeline[-1][0].year == 2026


def test_compute_histogram(analyzer):
    """Verifica o agrupamento por ano no histograma."""
    timeline = [
        (datetime(2022, 5, 1), "A", "desc"),
        (datetime(2022, 8, 1), "A", "desc"),
        (datetime(2023, 1, 1), "B", "desc"),
        (datetime(2025, 10, 1), "C", "desc"),
    ]
    hist = analyzer.compute_histogram(timeline)
    assert hist == {"2022": 2, "2023": 1, "2025": 1}


def test_detect_trend_growing(analyzer):
    """Verifica detecção de tendência crescente."""
    hist = {"2022": 1, "2023": 3, "2024": 5, "2025": 9}
    trend = analyzer.detect_trend(hist)
    assert trend == "crescente"


def test_detect_trend_falling(analyzer):
    """Verifica detecção de tendência decrescente."""
    hist = {"2022": 10, "2023": 7, "2024": 4, "2025": 1}
    trend = analyzer.detect_trend(hist)
    assert trend == "decrescente"


def test_detect_trend_stable(analyzer):
    """Verifica detecção de tendência estável."""
    hist = {"2022": 5, "2023": 5, "2024": 6, "2025": 5}
    trend = analyzer.detect_trend(hist)
    assert trend == "estável"


def test_detect_trend_insufficient(analyzer):
    """Verifica detecção com dados insuficientes."""
    assert analyzer.detect_trend({}) == "dados insuficientes"
    assert analyzer.detect_trend({"2024": 5}) == "dados insuficientes"


def test_generate_timeline_section(analyzer):
    """Garante que a seção markdown é formatada corretamente."""
    results = [
        SynthesizedResult(
            entity="A",
            title="Projeto A",
            description="Criado em 2024-01-01.",
            sources=["github"],
            urls=["http://example.com"],
            combined_score=75.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
        )
    ]
    markdown = analyzer.generate_timeline_section(results)
    assert "## 📅 Linha do Tempo & Análise Temporal" in markdown
    assert "Projeto A" in markdown
    assert "2024" in markdown
