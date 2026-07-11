"""Testes da Fase 5 — UI Allowlist/Denylist + Detecção de Query Vaga.

Valida:
  - `_is_query_too_vague()` extraída do hook anti_query_vaga para o ExpandStage.
  - Preenchimento de `trust_tier` e filtragem de fontes "deny" no SearchStage.
  - Boost de score para fontes "allow" no ScoreStage.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from src.pipeline.stages.expand_stage import _is_query_too_vague
from src.pipeline.stages.search_stage import SearchStage
from src.pipeline.stages.score_stage import ScoreStage
from src.types import RankedResult, SearchResult


# ── 5.1: Detecção de query vaga ──────────────────────────────────────────────


class TestIsQueryTooVague:
    """Testes da heurística de query vaga."""

    def test_short_query_is_vague(self) -> None:
        assert _is_query_too_vague("python")

    def test_query_under_15_chars_is_vague(self) -> None:
        assert _is_query_too_vague("linguagem x")

    def test_two_words_without_punctuation_is_vague(self) -> None:
        assert _is_query_too_vague("melhor ferramenta")

    def test_question_with_interrog_is_not_vague(self) -> None:
        assert not _is_query_too_vague("Como fazer X em Python?")

    def test_question_starter_is_not_vague(self) -> None:
        assert not _is_query_too_vague("qual o melhor CRM open source?")

    def test_substantive_multiword_is_not_vague(self) -> None:
        assert not _is_query_too_vague(
            "melhores ferramentas self-hosted para RAG com LLM em 2026"
        )

    def test_vague_only_word_is_vague(self) -> None:
        assert _is_query_too_vague("pesquisa")
        assert _is_query_too_vague("busca")

    def test_stack_trace_without_question_is_vague(self) -> None:
        assert _is_query_too_vague(
            "Traceback (most recent call last):\n  File main.py, line 1\nError: boom"
        )

    def test_empty_query_is_vague(self) -> None:
        assert _is_query_too_vague("")
        assert _is_query_too_vague("   ")

    def test_stack_trace_with_question_is_not_vague(self) -> None:
        assert not _is_query_too_vague(
            "Error: boom in main.py — como corrigir esse ImportError?"
        )


# ── 5.2: trust_tier no SearchStage ───────────────────────────────────────────


class TestSearchStageTrustRules:
    """Testes de allowlist/denylist no SearchStage."""

    def _make_search_stage(self) -> SearchStage:
        return SearchStage(searchers={}, cache=None, ranker=MagicMock())

    def _ctx_with_rules(self, rules: dict) -> MagicMock:
        ctx = MagicMock()
        ctx.extras = {"trust_rules": rules}
        return ctx

    def test_trust_tier_filled_from_rules(self, monkeypatch) -> None:
        # Desabilita filtro para inspecionar os tiers de ambas as fontes.
        monkeypatch.setenv("FILTER_DENIED_SOURCES", "false")
        stage = self._make_search_stage()
        results = [
            SearchResult(source="reddit", url="https://reddit.com/a"),
            SearchResult(source="github", url="https://github.com/b"),
        ]
        ctx = self._ctx_with_rules({"reddit": "allow", "github": "deny"})
        out = stage._apply_trust_rules(results, ctx)
        tiers = {r.source: r.trust_tier for r in out}
        assert tiers == {"reddit": "allow", "github": "deny"}

    def test_deny_source_filtered_by_default(self) -> None:
        stage = self._make_search_stage()
        results = [
            SearchResult(source="reddit", url="https://reddit.com/a"),
            SearchResult(source="blog-duvidoso.com", url="https://blog-duvidoso.com/x"),
        ]
        ctx = self._ctx_with_rules({"blog-duvidoso.com": "deny"})
        out = stage._apply_trust_rules(results, ctx)
        assert all(r.source != "blog-duvidoso.com" for r in out)
        assert len(out) == 1

    def test_deny_source_kept_when_filter_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("FILTER_DENIED_SOURCES", "false")
        stage = self._make_search_stage()
        results = [SearchResult(source="blog-duvidoso.com", url="https://x.com/y")]
        ctx = self._ctx_with_rules({"blog-duvidoso.com": "deny"})
        out = stage._apply_trust_rules(results, ctx)
        assert len(out) == 1
        assert out[0].trust_tier == "deny"

    def test_neutral_when_no_rules(self) -> None:
        stage = self._make_search_stage()
        results = [SearchResult(source="reddit", url="https://reddit.com/a")]
        ctx = self._ctx_with_rules({})
        out = stage._apply_trust_rules(results, ctx)
        assert out[0].trust_tier == "neutral"

    def test_no_rules_short_circuit(self) -> None:
        stage = self._make_search_stage()
        results = [SearchResult(source="reddit", url="https://reddit.com/a")]
        ctx = MagicMock()
        ctx.extras = {}
        out = stage._apply_trust_rules(results, ctx)
        assert out == results


# ── 5.2: boost de allow no ScoreStage ────────────────────────────────────────


class TestScoreStageAllowBoost:
    """Testes do boost de score para fontes 'allow' no ScoreStage."""

    def _score_pair(self, stage: ScoreStage, trust_tier: str) -> RankedResult:
        """Score uma resultado idêntico com o trust_tier dado (isolando o boost)."""
        r = RankedResult(
            source="reddit",
            url="https://reddit.com/a",
            title="Título neutro sem clickbait",
            description=(
                "Este é um texto com conteúdo razoável sobre a tecnologia, "
                "publicado em 2024, com citações http://exemplo.com/a e "
                "http://exemplo.com/b para sustentar a afirmação de forma "
                "consistente ao longo do parágrafo descritivo."
            ),
            trust_tier=trust_tier,
        )
        out = asyncio.run(stage.execute([r], context={"query": "q"}))
        return out[0]

    def test_allow_boost_increases_score(self) -> None:
        stage = ScoreStage()
        neutral = self._score_pair(stage, "neutral")
        allow = self._score_pair(stage, "allow")
        assert allow.score > neutral.score

    def test_neutral_unchanged(self) -> None:
        stage = ScoreStage()
        neutral = self._score_pair(stage, "neutral")
        # Sem allow, o score reflete apenas a heurística (determinístico).
        assert neutral.trust_tier == "neutral"
        assert 0.0 <= neutral.score <= 100.0

    def test_allow_boost_configurable_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ALLOW_SOURCE_SCORE_BOOST", "0.3")
        stage = ScoreStage()
        neutral = self._score_pair(stage, "neutral")
        allow = self._score_pair(stage, "allow")
        # Boost de 0.3 em escala 0-100 = +30 pontos no score.
        assert abs(allow.score - (neutral.score + 30.0)) < 1e-6

    def test_allow_boost_capped_at_100(self, monkeypatch) -> None:
        monkeypatch.setenv("ALLOW_SOURCE_SCORE_BOOST", "0.9")
        stage = ScoreStage()
        neutral = self._score_pair(stage, "neutral")
        allow = self._score_pair(stage, "allow")
        # score nunca estoura 100.
        assert allow.score <= 100.0
        # e sempre é >= ao score neutro (boost não negativo).
        assert allow.score >= neutral.score
