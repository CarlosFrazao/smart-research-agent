"""Testes de roteamento universal e de notícias (Fase 5 — Plano Parte 4).

Cobrem:

1. Query noticiosa é classificada em ``Domain.NEWS`` pelo ``IntentAnalyzer``
   heurístico e ativa fontes de notícia (ex: ``gdelt``) no ``SourcePlanner``.
2. O ``domains.yaml`` é carregado incluindo ``universal`` e ``news``.
3. A renderização da seção de perspectivas (``ReportGenerator``) não falha
   quando não há dados de tom.
"""

from pathlib import Path

import pytest

from src.intent_analyzer import IntentAnalyzer
from src.report_generator import ReportGenerator
from src.source_planner import SourcePlanner
from src.types import Domain, ExpandedQuery, IntentResult, Intention


class _StubLLM:
    """LLM client mínimo que nunca é chamado nos caminhos heurísticos."""

    async def generate(self, *args, **kwargs):
        raise AssertionError("LLM não deveria ser chamado neste teste")

    async def generate_structured(self, *args, **kwargs):
        raise AssertionError("LLM não deveria ser chamado neste teste")


def _make_intent(domain: Domain, query: str = "query generica") -> IntentResult:
    return IntentResult(
        domain=domain,
        entities=[],
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="media",
    )


def _make_planner() -> SourcePlanner:
    return SourcePlanner(config={}, llm=_StubLLM())


# ── 1. Roteamento de notícias pelo IntentAnalyzer ──────────────────────── #


@pytest.mark.parametrize(
    "query",
    [
        "O que aconteceu hoje na bolsa",
        "últimas notícias sobre a eleição",
        "breaking news sobre a guerra",
        "what happened today in the economy",
    ],
)
def test_news_query_routed_to_news_domain(query: str):
    analyzer = IntentAnalyzer(llm_client=None)
    intent = analyzer._heuristic_domain(query)
    assert intent == Domain.NEWS


def test_tech_query_not_routed_to_news():
    analyzer = IntentAnalyzer(llm_client=None)
    # "python" é tech explícito → não deve cair em NEWS nem confundir.
    intent = analyzer._heuristic_domain("como usar python para api rest")
    assert intent != Domain.NEWS


def test_ambiguous_query_prefers_general_over_news():
    analyzer = IntentAnalyzer(llm_client=None)
    # Tem palavra de notícia ("hoje") E tech ("api") → prioriza precisão técnica.
    intent = analyzer._heuristic_domain("notícia sobre nova api de python hoje")
    assert intent == Domain.GENERAL


# ── 2. domains.yaml inclui universal e news ───────────────────────────── #


def test_domains_yaml_includes_universal_and_news():
    planner = _make_planner()
    domain_map = planner.domain_map
    assert "universal" in domain_map
    assert "news" in domain_map
    # Fontes da Fase 2 presentes no domínio news.
    news_primary = domain_map["news"].get("primary", [])
    assert "gdelt" in news_primary
    assert "google_news_rss" in news_primary
    assert "newsapi_org" in news_primary


# ── 3. SourcePlanner usa fontes de notícia para domínio NEWS ──────────── #


def test_source_planner_uses_gdelt_for_news_domain():
    planner = _make_planner()
    intent = _make_intent(Domain.NEWS, query="o que aconteceu hoje na bolsa")
    plan = planner.plan(
        intent,
        [ExpandedQuery(query="o que aconteceu hoje na bolsa", type="original")],
    )
    assert "gdelt" in plan.primary
    assert "google_news_rss" in plan.primary


def test_source_planner_routes_news_query_from_general_fallback():
    planner = _make_planner()
    # Domínio GENERAL + query noticiosa (sem tech) → promovido a news.
    intent = _make_intent(Domain.GENERAL, query="últimas notícias da economia hoje")
    plan = planner.plan(
        intent,
        [ExpandedQuery(query="últimas notícias da economia hoje", type="original")],
    )
    assert "gdelt" in plan.primary


# ── 4. Renderização de perspectivas sem dados de tom ─────────────────── #


def test_perspectives_section_empty_without_tone_data():
    gen = ReportGenerator(llm_client=_StubLLM())
    # Resultados sem campo tone → seção vazia, não quebra.
    results = [
        type(
            "R",
            (),
            {
                "title": "Sem tom",
                "metrics": {},
                "sources": ["reddit"],
                "combined_score": 50.0,
            },
        )()
    ]
    section = gen._build_perspectives_section("query teste", results)
    assert section == ""


def test_perspectives_section_renders_contrast():
    gen = ReportGenerator(llm_client=_StubLLM())
    # Dois resultados com tons contrastantes → seção renderizada.
    class _R:
        def __init__(self, title, tone, source):
            self.title = title
            self.metrics = {"tone": tone}
            self.sources = [source]
            self.combined_score = 50.0

    results = [
        _R("Cobertura favorável", 5.0, "gdelt"),
        _R("Cobertura crítica", -5.0, "bluesky"),
    ]
    section = gen._build_perspectives_section("evento политico", results)
    assert "Espectro de Perspectivas" in section
    assert "Favorável" in section
    assert "Crítico" in section


def test_perspectives_section_low_contrast_omitted():
    gen = ReportGenerator(llm_client=_StubLLM())

    class _R:
        def __init__(self, tone):
            self.title = "x"
            self.metrics = {"tone": tone}
            self.sources = ["gdelt"]
            self.combined_score = 50.0

    # Tons muito próximos (amplitude < 2.0) → omitir seção.
    results = [_R(1.0), _R(2.0)]
    assert gen._build_perspectives_section("query", results) == ""
