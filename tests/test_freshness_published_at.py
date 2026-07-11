"""Testes unitários do mecanismo de freshness com published_at.

Cobrem o uso de ``SearchResult.published_at`` (Fase 1) no
``HybridRanker._compute_freshness``, garantindo:

1. Fallback para ``fetched_at`` quando ``published_at`` é None.
2. Uso de ``published_at`` quando disponível.
3. Robustez naive vs aware (não deve dar crash de timezone).
4. Boost para itens recentes e decaimento por meia-vida da fonte
   (inclusive as novas fontes de notícia da Fase 2).
"""

from datetime import UTC, datetime

from src.ranking.hybrid_ranker import FRESHNESS_HALFLIFE, HybridRanker
from src.types import SearchResult


def _make_result(source: str, published_at=None, fetched_at=None) -> SearchResult:
    return SearchResult(
        source=source,
        title=f"item-{source}",
        url=f"https://{source}.example/{source}",
        published_at=published_at,
        fetched_at=fetched_at or datetime.now(UTC),
    )


def _ranker() -> HybridRanker:
    return HybridRanker()


# now fixo (aware UTC) para tornar os testes determinísticos
NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


def test_published_at_field_exists():
    """O campo published_at existe e é None por padrão (backward compat)."""
    r = SearchResult(source="github", title="t", url="https://x")
    assert hasattr(r, "published_at")
    assert r.published_at is None


def test_freshness_falls_back_to_fetched_at():
    """Sem published_at, freshness usa fetched_at como referência."""
    ranker = _ranker()
    fetched = NOW - __import__("datetime").timedelta(days=5)
    r = _make_result("default", fetched_at=fetched)

    score = ranker._compute_freshness(r, NOW)

    # halflife default = 30; age 5d; dentro do boost (<=30d) -> *1.3
    import math

    expected = min(1.0, math.exp(-5.0 / 30.0) * 1.3)
    assert 0.0 <= score <= 1.0
    assert abs(score - expected) < 1e-9


def test_freshness_uses_published_at_when_present():
    """Com published_at presente (mais antigo), ele prevalece sobre fetched_at."""
    ranker = _ranker()
    published = NOW - __import__("datetime").timedelta(days=10)
    fetched = NOW - __import__("datetime").timedelta(days=1)
    older = _make_result("reddit", published_at=published, fetched_at=fetched)

    score_with_published = ranker._compute_freshness(older, NOW)

    # Baseline: só fetched_at (1 dia) — deveria ser mais fresco
    baseline = _make_result("reddit", fetched_at=fetched)
    score_baseline = ranker._compute_freshness(baseline, NOW)

    assert score_with_published < score_baseline


def test_freshness_handles_naive_datetime_without_crash():
    """Datetime naive (sem tzinfo) não deve gerar TypeError nem crash."""
    ranker = _ranker()
    # naive datetime (como costuma vir de JSON/legacy)
    naive = datetime(2026, 7, 9, 12, 0, 0)
    r = _make_result("default", published_at=naive)

    try:
        score = ranker._compute_freshness(r, NOW)
    except TypeError:
        raise AssertionError("TypeError ao comparar naive vs aware datetime")

    assert 0.0 <= score <= 1.0


def test_freshness_recent_gets_boost_over_old():
    """Item de 1 hora é mais fresco que item de 60 dias (mesma fonte)."""
    ranker = _ranker()
    recent = _make_result(
        "default", published_at=NOW - __import__("datetime").timedelta(hours=1)
    )
    old = _make_result(
        "default", published_at=NOW - __import__("datetime").timedelta(days=60)
    )

    score_recent = ranker._compute_freshness(recent, NOW)
    score_old = ranker._compute_freshness(old, NOW)

    assert score_recent > score_old
    # recente (1h, halflife 30d) recebe boost e satura perto de 1.0
    assert score_recent > 0.9


def test_freshness_halflife_by_source():
    """Mesma idade, fontes com meia-vida diferentes -> scores diferentes.

    github (90d) permanece mais fresco que rss (3d) para conteúdo de 2 dias.
    """
    ranker = _ranker()
    age = __import__("datetime").timedelta(days=2)
    gh = _make_result("github", published_at=NOW - age)
    rss = _make_result("rss", published_at=NOW - age)

    score_gh = ranker._compute_freshness(gh, NOW)
    score_rss = ranker._compute_freshness(rss, NOW)

    assert score_gh > score_rss


def test_freshness_news_halflife_decays_fast():
    """Fontes de notícia (gdelt 0.5d) decaem rápido vs github (90d)."""
    ranker = _ranker()
    age = __import__("datetime").timedelta(days=1)
    gdelt = _make_result("gdelt", published_at=NOW - age)
    gh = _make_result("github", published_at=NOW - age)

    score_gdelt = ranker._compute_freshness(gdelt, NOW)
    score_gh = ranker._compute_freshness(gh, NOW)

    assert FRESHNESS_HALFLIFE["gdelt"] == 0.5
    assert score_gdelt < score_gh


def test_freshness_unparseable_timestamp_returns_neutral():
    """Timestamp não-parseável (string lixo) cai no except e retorna 0.5.

    O ``_compute_freshness`` usa ``getattr``, então um objeto mínimo com
    ``published_at``/``fetched_at`` expondo um valor não-convertível exercita
    o branch defensivo sem depender da validação do Pydantic.
    """

    class _BadResult:
        source = "default"
        published_at = "not-a-real-datetime"
        fetched_at = "also-broken"

    ranker = _ranker()
    score = ranker._compute_freshness(_BadResult(), NOW)
    assert score == 0.5
