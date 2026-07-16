"""Onda 3 / M3.1 — SLAs de timeout adaptativos por modo (Gap G).

Modos hardcore (black_ops) usam proxies pesados e cascata de scrapers, então
precisam de timeout mais folgado para não estrangular conectores lentos-porém-
completos (github/reddit davam TimeoutError). Modos rápidos apertam o SLA.

Ver: Plan_SRA_Melhorias_Qualidade_2026-07-16.md
"""

from src.pipeline.stages.search_stage import (
    SOURCE_TIMEOUT_MAP,
    TIMEOUT_MODE_MULTIPLIER,
    get_timeout_for_source,
)


def test_black_ops_expands_timeout():
    base = get_timeout_for_source("github")
    hardcore = get_timeout_for_source(
        "github", TIMEOUT_MODE_MULTIPLIER["black_ops"]
    )
    assert hardcore > base
    assert hardcore == base * TIMEOUT_MODE_MULTIPLIER["black_ops"]


def test_guerrilha_tightens_timeout():
    base = get_timeout_for_source("github")
    fast = get_timeout_for_source("github", TIMEOUT_MODE_MULTIPLIER["guerrilha"])
    assert fast < base


def test_default_multiplier_is_base():
    assert get_timeout_for_source("github", 1.0) == SOURCE_TIMEOUT_MAP["github"]
    # Sem multiplicador explícito = 1.0.
    assert get_timeout_for_source("github") == SOURCE_TIMEOUT_MAP["github"]


def test_invalid_multiplier_falls_back_to_base():
    base = get_timeout_for_source("github")
    assert get_timeout_for_source("github", -5) == base
    assert get_timeout_for_source("github", 0) == base
    assert get_timeout_for_source("github", "bad") == base  # type: ignore[arg-type]


def test_unknown_source_uses_default_scaled():
    # Fonte não mapeada e não-confiável cai no _default_scraping, escalado.
    from src.pipeline.stages.search_stage import UNTRUSTED_SOURCES

    src = next(iter(UNTRUSTED_SOURCES))
    base = SOURCE_TIMEOUT_MAP["_default_scraping"]
    assert get_timeout_for_source(src, 2.0) == base * 2.0


def test_all_modes_have_positive_multiplier():
    for mode, mult in TIMEOUT_MODE_MULTIPLIER.items():
        assert mult > 0, f"multiplicador do modo {mode} deve ser positivo"
