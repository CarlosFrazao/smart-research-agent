"""Tests for src/comparator.py — Bloco 4.3."""

import pytest
from unittest.mock import MagicMock

from src.comparator import (
    Comparator,
    EntityProfile,
    _recency_label,
    _sentiment_label,
    _clean_entity,
)
from src.types import SynthesizedResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_result(
    title: str = "Result",
    snippet: str = "",
    url: str = "https://example.com",
    sources: list | None = None,
    score: float = 0.7,
    stars: int = 0,
    recency_score: float = 0.5,
    sentiment_score: float = 0.0,
) -> SynthesizedResult:
    r = MagicMock(spec=SynthesizedResult)
    r.title = title
    r.snippet = snippet
    r.description = snippet
    r.url = url
    r.urls = [url]
    r.sources = sources or ["github"]
    r.score = score
    r.combined_score = score
    r.metrics = {
        "stars": stars,
        "recency_score": recency_score,
        "sentiment_score": sentiment_score,
    }
    return r


# ---------------------------------------------------------------------------
# detect_comparison_query
# ---------------------------------------------------------------------------

class TestDetectComparisonQuery:
    comp = Comparator()

    def test_vs_pattern(self):
        ok, entities = self.comp.detect_comparison_query("Python vs JavaScript")
        assert ok is True
        assert "Python" in entities
        assert "JavaScript" in entities

    def test_versus_pattern(self):
        ok, entities = self.comp.detect_comparison_query("React versus Vue")
        assert ok is True
        assert len(entities) == 2

    def test_ou_pattern_pt(self):
        ok, entities = self.comp.detect_comparison_query("Django ou FastAPI")
        assert ok is True
        assert "Django" in entities
        assert "FastAPI" in entities

    def test_x_pattern_pt(self):
        ok, entities = self.comp.detect_comparison_query("PostgreSQL x MySQL")
        assert ok is True

    def test_no_comparison(self):
        ok, entities = self.comp.detect_comparison_query("how to install python")
        assert ok is False
        assert entities == []

    def test_empty_query(self):
        ok, entities = self.comp.detect_comparison_query("")
        assert ok is False

    def test_qual_e_melhor(self):
        ok, entities = self.comp.detect_comparison_query("qual é melhor Python ou Ruby?")
        assert ok is True


# ---------------------------------------------------------------------------
# build_entity_profiles
# ---------------------------------------------------------------------------

class TestBuildEntityProfiles:
    comp = Comparator()

    def test_basic_matching(self):
        results = [
            make_result(title="Intro to Python", score=0.8, stars=5000),
            make_result(title="Why use JavaScript", score=0.6, stars=2000),
            make_result(title="Python frameworks", score=0.9, stars=3000),
        ]
        profiles = self.comp.build_entity_profiles(["Python", "JavaScript"], results)
        assert len(profiles) == 2
        py = next(p for p in profiles if p.name == "Python")
        js = next(p for p in profiles if p.name == "JavaScript")
        assert py.result_count == 2
        assert js.result_count == 1
        assert py.total_stars == 8000

    def test_no_match_returns_empty_profile(self):
        results = [make_result(title="Rust programming", score=0.7)]
        profiles = self.comp.build_entity_profiles(["Python", "Go"], results)
        for p in profiles:
            assert p.result_count == 0

    def test_avg_score_calculated(self):
        results = [
            make_result(title="Python A", score=0.6),
            make_result(title="Python B", score=0.8),
        ]
        profiles = self.comp.build_entity_profiles(["Python"], results)
        assert abs(profiles[0].avg_score - 0.7) < 0.01

    def test_case_insensitive_match(self):
        results = [make_result(title="PYTHON tutorial", score=0.5)]
        profiles = self.comp.build_entity_profiles(["python"], results)
        assert profiles[0].result_count == 1


# ---------------------------------------------------------------------------
# generate_comparison_section
# ---------------------------------------------------------------------------

class TestGenerateComparisonSection:
    comp = Comparator()

    def test_non_comparative_query_returns_empty(self):
        results = [make_result()]
        section = self.comp.generate_comparison_section("best python libraries", results)
        assert section == ""

    def test_comparative_query_returns_table(self):
        results = [
            make_result(title="Django web framework", score=0.85, stars=65000),
            make_result(title="FastAPI modern", score=0.80, stars=60000),
        ]
        section = self.comp.generate_comparison_section("Django vs FastAPI", results)
        assert "## ⚖️ Comparação Side-by-Side" in section
        assert "Django" in section
        assert "FastAPI" in section
        assert "|" in section  # table present

    def test_table_contains_score_row(self):
        results = [
            make_result(title="React framework", score=0.75),
            make_result(title="Vue framework", score=0.65),
        ]
        section = self.comp.generate_comparison_section("React vs Vue", results)
        assert "Score médio" in section

    def test_recommendation_shown_when_clear_winner(self):
        results = [
            make_result(title="Python is great", score=0.95, stars=100000),
            make_result(title="Ruby basics", score=0.20, stars=10),
        ]
        section = self.comp.generate_comparison_section("Python vs Ruby", results)
        assert "Recomendação" in section


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def test_recency_label_recent():
    assert "Recente" in _recency_label(0.9)

def test_recency_label_moderate():
    assert "Moderado" in _recency_label(0.5)

def test_recency_label_old():
    assert "Desatualizado" in _recency_label(0.1)

def test_sentiment_label_positive():
    assert "Positivo" in _sentiment_label(0.5)

def test_sentiment_label_negative():
    assert "Negativo" in _sentiment_label(-0.5)

def test_sentiment_label_neutral():
    assert "Neutro" in _sentiment_label(0.0)

def test_clean_entity_strips_articles():
    assert _clean_entity("the Python") == "Python"
    assert _clean_entity("o Django") == "Django"

def test_clean_entity_strips_punctuation():
    assert _clean_entity("FastAPI,") == "FastAPI"
