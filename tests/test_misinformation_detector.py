"""Tests for src/misinformation_detector.py — Bloco 4.4."""

import os
import tempfile
import pytest
import yaml

from src.misinformation_detector import MisinformationDetector


@pytest.fixture
def temp_yaml_config():
    data = {
        "misinformation_domains": [
            {"domain": "badtech.com", "reason": "Fake Tech", "penalty": 0.2},
            {"domain": "unreliable.org", "reason": "SEO farm", "penalty": 0.6},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump(data, f)
        temp_path = f.name
    
    yield temp_path
    
    if os.path.exists(temp_path):
        os.remove(temp_path)


def test_detector_loads_correctly(temp_yaml_config):
    detector = MisinformationDetector(config_path=temp_yaml_config)
    assert len(detector.domains) == 2
    assert "badtech.com" in detector.domains
    assert detector.domains["badtech.com"]["penalty"] == 0.2


def test_detector_check_clean_url(temp_yaml_config):
    detector = MisinformationDetector(config_path=temp_yaml_config)
    flagged, penalty, reason = detector.check_url("https://github.com/python/cpython")
    assert flagged is False
    assert penalty == 1.0
    assert reason == ""


def test_detector_check_flagged_url(temp_yaml_config):
    detector = MisinformationDetector(config_path=temp_yaml_config)
    flagged, penalty, reason = detector.check_url("http://badtech.com/news/article-1")
    assert flagged is True
    assert penalty == 0.2
    assert reason == "Fake Tech"


def test_detector_check_subdomain(temp_yaml_config):
    detector = MisinformationDetector(config_path=temp_yaml_config)
    flagged, penalty, reason = detector.check_url("https://subdomain.unreliable.org/blog/index.html")
    assert flagged is True
    assert penalty == 0.6
    assert reason == "SEO farm"


def test_detector_invalid_file():
    detector = MisinformationDetector(config_path="non_existent_file.yaml")
    assert len(detector.domains) == 0
    flagged, penalty, reason = detector.check_url("http://anydomain.com")
    assert flagged is False
    assert penalty == 1.0


def test_detector_invalid_url(temp_yaml_config):
    detector = MisinformationDetector(config_path=temp_yaml_config)
    flagged, penalty, reason = detector.check_url("")
    assert flagged is False
    assert penalty == 1.0


@pytest.mark.asyncio
async def test_ranker_integration_with_misinformation(temp_yaml_config):
    from src.ranker import QualityRanker
    from src.types import SearchResult

    # Inicializa ranker
    ranker = QualityRanker()
    # Substitui o detector padrão do ranker pelo nosso detector de teste
    ranker.detector = MisinformationDetector(config_path=temp_yaml_config)

    # Cria dois resultados idênticos exceto a URL (um limpo e um de desinformação)
    clean_result = SearchResult(
        source="github",
        title="Valid Project",
        url="https://github.com/valid/project",
        description="A great open source library",
        metrics={"stars": 1000, "forks": 100, "language": "Python", "license": "MIT", "updated_at": "2026-01-01T00:00:00Z"},
    )
    
    bad_result = SearchResult(
        source="github",
        title="Spam Project",
        url="https://badtech.com/news/123",
        description="A great open source library",
        metrics={"stars": 1000, "forks": 100, "language": "Python", "license": "MIT", "updated_at": "2026-01-01T00:00:00Z"},
    )

    ranked = await ranker.rank([clean_result, bad_result])
    assert len(ranked) == 2

    # A ordem deve ser o projeto limpo primeiro, pois o ruim foi penalizado
    assert ranked[0].title == "Valid Project"
    assert ranked[1].title == "Spam Project"

    # Verifica os scores
    clean_score = ranked[0].score
    bad_score = ranked[1].score

    # bad_score deve ser exatamente clean_score * 0.2
    assert abs(bad_score - round(clean_score * 0.2, 2)) < 0.01
    
    # Verifica o score breakdown
    assert ranked[1].score_breakdown["misinformation_penalty"] == 0.2
    assert ranked[1].score_breakdown["misinformation_reason"] == "Fake Tech"
    assert ranked[0].score_breakdown["misinformation_penalty"] == 1.0

