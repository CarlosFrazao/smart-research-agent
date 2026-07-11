"""Testes de estimativa de custo pré-busca (Fase 4).

Valida:
  1. `TokenEconomy.get_avg_cost_per_source` — catálogo de custos por fonte.
  2. `estimate_search_cost` — soma custos do SourcePlan × n_queries.
  3. `ScoreStage` propaga max() do cluster (regra da Fase 4).
"""

from __future__ import annotations

import pytest

from src.token_economy import TokenEconomy
from src.pipeline.stages.expand_stage import estimate_search_cost
from src.types import SourcePlan, ExpandedQuery
from src.pipeline.stages.score_stage import ScoreStage
from src.types import RankedResult


class TestTokenEconomyCostPerSource:
    """Testa o catálogo de custos por fonte."""

    def test_free_sources_zero_cost(self):
        te = TokenEconomy()
        for src in ("github", "npm", "pypi", "reddit", "hackernews", "arxiv", "web"):
            assert te.get_avg_cost_per_source(src) == 0.0

    def test_paid_sources_have_cost(self):
        te = TokenEconomy()
        assert te.get_avg_cost_per_source("firecrawl") > 0.0
        assert te.get_avg_cost_per_source("newsapi") > 0.0
        assert te.get_avg_cost_per_source("google") > 0.0

    def test_unknown_source_default_zero(self):
        te = TokenEconomy()
        # Fonte não catalogada → assume-se gratuita (nunca superestima)
        assert te.get_avg_cost_per_source("some_future_source") == 0.0

    def test_costs_are_floats(self):
        te = TokenEconomy()
        assert isinstance(te.get_avg_cost_per_source("firecrawl"), float)


class TestEstimateSearchCost:
    """Testa a estimativa agregada do SourcePlan."""

    def _make_plan(self, primary, secondary):
        return SourcePlan(
            sources={},
            primary=primary,
            secondary=secondary,
        )

    def test_empty_plan_zero_cost(self):
        te = TokenEconomy()
        plan = self._make_plan([], [])
        assert estimate_search_cost(plan, te, n_queries=3) == 0.0

    def test_only_free_sources(self):
        te = TokenEconomy()
        plan = self._make_plan(["github", "reddit", "hackernews"], ["arxiv"])
        # Todas gratuitas → custo zero
        assert estimate_search_cost(plan, te, n_queries=2) == 0.0

    def test_paid_sources_sum(self):
        te = TokenEconomy()
        plan = self._make_plan(["firecrawl", "newsapi"], [])
        # firecrawl=0.005, newsapi=0.0005 → por 1 query = 0.0055
        cost = estimate_search_cost(plan, te, n_queries=1)
        assert abs(cost - (0.005 + 0.0005)) < 1e-9

    def test_scales_with_n_queries(self):
        te = TokenEconomy()
        plan = self._make_plan(["firecrawl"], [])
        cost_1 = estimate_search_cost(plan, te, n_queries=1)
        cost_5 = estimate_search_cost(plan, te, n_queries=5)
        assert abs(cost_5 - cost_1 * 5) < 1e-9

    def test_dedup_sources(self):
        """Fontes duplicadas no plano não devem ser contadas duas vezes."""
        te = TokenEconomy()
        # primary e secondary com sobreposição
        plan = self._make_plan(["firecrawl", "github"], ["firecrawl", "newsapi"])
        cost = estimate_search_cost(plan, te, n_queries=1)
        # firecrawl aparece 2x mas é deduplicado → 0.005 + 0.0 + 0.0005 = 0.0055
        assert abs(cost - (0.005 + 0.0005)) < 1e-9

    def test_none_inputs_safe(self):
        te = TokenEconomy()
        assert estimate_search_cost(None, te, n_queries=1) == 0.0
        assert estimate_search_cost(self._make_plan(["firecrawl"], []), None, n_queries=1) == 0.0


class TestScoreStageClusterMax:
    """Testa a regra da Fase 4: ScoreStage usa max() do cluster."""

    def _make_ranked(
        self, source: str, score: float, cluster_id: str | None = None
    ) -> RankedResult:
        r = RankedResult(
            source=source,
            url=f"https://{source}.com/{score}",
            title=f"{source} result",
            description="conteúdo",
            score=score,
        )
        r.cluster_id = cluster_id
        r.confidence_score = score / 100.0
        return r

    @pytest.mark.asyncio
    async def test_cluster_max_propagated(self):
        """O max() do cluster deve propagar para todos os membros."""
        stage = ScoreStage()
        # Cluster com scores 90 e 40 → max = 90
        r1 = self._make_ranked("reddit", 90.0, "cluster_0")
        r2 = self._make_ranked("hackernews", 40.0, "cluster_0")
        # Não-clusterizado
        r3 = self._make_ranked("github", 70.0, None)

        scored = await stage.execute([r1, r2, r3], context={})
        by_source = {r.source: r for r in scored}

        # Ambos do cluster devem ter score = 90 (max)
        assert by_source["reddit"].score == 90.0
        assert by_source["hackernews"].score == 90.0
        # Não-clusterizado mantém seu score
        assert by_source["github"].score == 70.0

    @pytest.mark.asyncio
    async def test_cluster_max_propagated_confidence(self):
        """confidence_score também reflete o max do cluster."""
        stage = ScoreStage()
        r1 = self._make_ranked("reddit", 80.0, "cluster_x")
        r2 = self._make_ranked("news", 55.0, "cluster_x")

        scored = await stage.execute([r1, r2], context={})
        for r in scored:
            assert r.confidence_score >= 0.8  # max/100 = 0.8
