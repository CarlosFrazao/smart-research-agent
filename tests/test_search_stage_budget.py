"""Épico B — Budget de tempo por fonte (B2/F5).

Garante que o teto HARD ``per_source_time_budget`` limita o timeout resolvido
por categoria×modo, impedindo que uma fonte lenta ocupe a pipeline além do
orçamento mesmo sob o multiplicador 2.2x do ``black_ops``.

Ver: Plan_SRA_BlackOps_Resiliencia_2026-07-18.md (Épico B / B2).
"""

import asyncio

from src.pipeline.stages.search_stage import (
    SearchStage,
    SearchStageConfig,
    get_timeout_for_source,
)


def test_black_ops_budget_caps_firecrawl_at_30s():
    """Black_ops define per_source_time_budget=30s; firecrawl (base 30s × 2.2)
    seria 66s sem o teto — o budget deve travar em 30s."""
    cfg = SearchStageConfig(
        timeout_multiplier=2.2,  # black_ops
        per_source_time_budget=30.0,
    )
    # Simula o cálculo de _search_with_timeout antes do wait_for.
    timeout = get_timeout_for_source("firecrawl", multiplier=cfg.timeout_multiplier)
    budget = cfg.per_source_time_budget
    if isinstance(budget, (int, float)) and budget > 0:
        timeout = min(float(timeout), float(budget))
    assert timeout == 30.0
    # Prova que sem o teto passaria de 30s (66s).
    assert get_timeout_for_source("firecrawl", 2.2) > 30.0


def test_budget_none_lets_category_timeout_through():
    """Sem budget (modos não-hardcore), o timeout de categoria vale integral."""
    cfg = SearchStageConfig(timeout_multiplier=1.0, per_source_time_budget=None)
    timeout = get_timeout_for_source("firecrawl", multiplier=cfg.timeout_multiplier)
    budget = cfg.per_source_time_budget
    if isinstance(budget, (int, float)) and budget > 0:
        timeout = min(float(timeout), float(budget))
    assert timeout == 30.0  # base firecrawl, sem teto adicional


def test_budget_lower_than_category_wins():
    """Budget menor que o SLA de categoria deve prevalecer."""
    cfg = SearchStageConfig(timeout_multiplier=1.0, per_source_time_budget=5.0)
    timeout = get_timeout_for_source("github", multiplier=cfg.timeout_multiplier)
    budget = cfg.per_source_time_budget
    if isinstance(budget, (int, float)) and budget > 0:
        timeout = min(float(timeout), float(budget))
    assert timeout == 5.0


async def test_search_with_timeout_respects_budget():
    """_search_with_timeout corta em `budget` mesmo se o searcher travar."""

    class _SlowSearcher:
        async def search(self, query, domain=None):
            await asyncio.sleep(10.0)  # mais que o budget
            return []

    cfg = SearchStageConfig(timeout_multiplier=1.0, per_source_time_budget=0.2)
    stage = SearchStage(searchers={}, cache=None, ranker=None, config=cfg)

    import time

    start = time.monotonic()
    try:
        await stage._search_with_timeout(_SlowSearcher(), "q", "infra", "slow")
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # cortou no budget de 0.2s, não nos 10s do searcher
        return
    raise AssertionError("esperado TimeoutError do budget")
