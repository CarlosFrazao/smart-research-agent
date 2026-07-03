import pytest
from datetime import datetime
from src.research_score import ResearchScoreAggregator, ResearchScore
from src.types import RankedResult, ResearchMetadata, SynthesizedResult
from src.peer_review_agent import PeerReviewReport, ReviewIssue

@pytest.fixture
def aggregator():
    return ResearchScoreAggregator()

@pytest.fixture
def base_metadata():
    return ResearchMetadata(
        query="test",
        domain="general",
        sources=["github"],
        total_results=1,
        iterations=1,
        timestamp=datetime.now(),
        duration_seconds=1.0,
    )

def test_dado_scores_grandes_quando_calcular_qualidade_entao_normaliza_escala_100(aggregator, base_metadata):
    # Arrange - combined_score na escala 0-100 (ex: 85.0)
    res = SynthesizedResult(
        entity="test",
        title="test title",
        description="test description",
        sources=["github"],
        urls=["https://github.com"],
        combined_score=85.0,
        metrics={},
        highlights=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
    )
    # Act
    score = aggregator.calculate(
        results=[res],
        metadata=base_metadata,
        all_raw_results=[],
    )
    # Assert
    assert score.quality == 0.85

def test_dado_scores_em_escala_correta_quando_calcular_qualidade_entao_mantem_escala_original(aggregator, base_metadata):
    # Arrange - score na escala 0.0-1.0
    res = SynthesizedResult(
        entity="test",
        title="test title",
        description="test description",
        sources=["github"],
        urls=["https://github.com"],
        combined_score=0.85,
        metrics={},
        highlights=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
    )
    # Act
    score = aggregator.calculate(
        results=[res],
        metadata=base_metadata,
        all_raw_results=[],
    )
    # Assert
    assert score.quality == 0.85

def test_dado_score_overall_extremo_quando_calcular_entao_clampa_overall_entre_zero_e_um(aggregator, base_metadata):
    # Arrange - se quality for > 1.0 (antes do bugfix), overall ficaria > 1.0
    # Forçamos isso passando um resultado com combined_score alto
    res = SynthesizedResult(
        entity="test",
        title="test title",
        description="test description",
        sources=["github"],
        urls=["https://github.com"],
        combined_score=500.0,  # Score absurdo
        metrics={},
        highlights=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
    )
    # Act
    score = aggregator.calculate(
        results=[res],
        metadata=base_metadata,
        all_raw_results=[],
    )
    # Assert
    assert score.overall <= 1.0
    assert score.overall >= 0.0

def test_dado_valor_alto_quando_gerar_barra_entao_barra_nao_ultrapassa_tamanho_maximo(aggregator):
    # Arrange
    score_overflow = ResearchScore(
        coverage=1.0,
        diversity=1.0,
        quality=79.11,  # Valor com overflow
        reliability=1.0,
        recency=1.0,
        conflicts=0,
        gaps=0,
        overall=20.47,
        grade="A+",
        total_sources_used=1,
        total_results_analyzed=1,
        total_claims_verified=1,
        total_claims_unverified=0,
    )
    # Act
    report_md = aggregator._format_score_block(score_overflow)

    # Assert
    # A barra de qualidade deve ter exatamente 10 caracteres de preenchimento (ex: ██████████)
    # Não pode ter dezenas de caracteres nem quebrar a tabela do markdown
    lines = report_md.split("\n")
    quality_line = [l for l in lines if "Qualidade" in l][0]
    # A linha se parece com: | Qualidade | 7911% | ██████████ |
    parts = [p.strip() for p in quality_line.split("|") if p.strip()]
    bar = parts[2]
    assert len(bar) == 10
    assert bar == "██████████"

def test_dado_issues_do_peer_review_quando_calcular_grade_entao_aplica_penalidade_proporcional(aggregator, base_metadata):
    # Arrange
    res = SynthesizedResult(
        entity="test",
        title="test title",
        description="test description",
        sources=["github"],
        urls=["https://github.com"],
        combined_score=98.0,  # Daria A+ limpo
        metrics={},
        highlights=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
        evidence_quality="verified",
    )

    # Criamos um relatório de peer review com 10 issues minor
    issues = [
        ReviewIssue(
            category="missing_context",
            severity="minor",
            description=f"Seção vazia {i}",
            location="Seção",
            suggestion="Expandir"
        )
        for i in range(10)
    ]
    peer_report = PeerReviewReport(
        overall_assessment="moderate",
        confidence_in_report=0.70,
        issues=issues,
        strengths=[],
        recommendations=[]
    )

    # Act
    score_clean = aggregator.calculate(
        results=[res],
        metadata=base_metadata,
        all_raw_results=[],
        planned_sources=["github"],
        peer_review_report=None
    )
    score_penalized = aggregator.calculate(
        results=[res],
        metadata=base_metadata,
        all_raw_results=[],
        planned_sources=["github"],
        peer_review_report=peer_report
    )

    # Assert
    # Com 10 issues minor, penalidade é 10 * 2% = 20%
    assert score_clean.grade == "A+"
    assert score_penalized.overall == pytest.approx(score_clean.overall - 0.20, abs=0.01)
    assert score_penalized.grade in ["C", "D"]
