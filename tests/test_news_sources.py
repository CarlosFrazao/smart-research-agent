"""Testes de conformidade das fontes de notícia (Plano Parte 4 — Fase 2).

Cobre as 5 fontes novas: GDELT, NewsAPI.org, Bluesky (GenericAPISearcher
estendido) e Google News RSS (GenericFeedSearcher). Valida que cada fonte:
  1. resolve o result_path / feed e produz resultados;
  2. popula `published_at` (campo crítico da Fase 1);
  3. popula `source` com o id correto;
  4. (NewsAPI) degrada graciosamente sem a API key.

As APIs live são mockadas com fixtures reais (capturadas por chamadas reais
ou fiéis ao schema documentado de cada API), sem dependência de rede.
"""

import json
from pathlib import Path

import pytest

from src.search.generic_api_searcher import (
    GenericAPISearcher,
    parse_flexible_date,
)
from src.search.generic_feed_searcher import GenericFeedSearcher

FIXTURES = Path("tests/fixtures/news_sources")


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def _fake_api_request(self, method, url, **kwargs):
    """Substitui _http_request do BaseSearcher por um JSON de fixture em disco.

    Decide o fixture pela URL (gdelts => gdelt.json, newsapi => newsapi, bsky => bluesky).
    """
    import httpx

    low = url.lower()
    if "gdeltproject.org" in low:
        payload = _load_fixture("gdelt.json")
    elif "newsapi.org" in low:
        payload = _load_fixture("newsapi_org.json")
    elif "bsky.app" in low or "bsky" in low:
        payload = _load_fixture("bluesky.json")
    else:
        payload = {}
    # httpx.Response exigiria transporte; devolvemos um objeto mínimo compatível
    # com o uso em GenericAPISearcher.search (apenas .json()).
    class _Resp:
        def json(self):
            return payload

    return _Resp()


@pytest.fixture
def patch_http(monkeypatch):
    """Aplica o monkeypatch de _http_request em GenericAPISearcher.

    Também injeta NEWSAPI_KEY para que a NewsAPI (auth_type=query_api_key,
    requires_api_key=true) não seja pulada por falta de credencial.
    """
    import src.search.generic_api_searcher as mod

    monkeypatch.setenv("NEWSAPI_KEY", "test-key-123")
    monkeypatch.setattr(mod.GenericAPISearcher, "_http_request", _fake_api_request)
    yield


# ───────────────────────── GDELT ─────────────────────────
@pytest.mark.asyncio
async def test_gdelt_populates_published_at(patch_http):
    searcher = GenericAPISearcher("gdelt")
    results = await searcher.search("inteligencia artificial")
    assert results, "GDELT deve retornar resultados do fixture"
    assert all(r.source == "gdelt" for r in results)
    assert all(r.published_at is not None for r in results), (
        "Fase 1 exige published_at populado no GDELT"
    )
    assert results[0].url == "https://example.com/news/gdelt-article-1"
    assert results[0].metrics.get("domain") == "example.com"
    assert results[0].metrics.get("tone") == "-1.234"


# ───────────────────────── NewsAPI ─────────────────────────
@pytest.mark.asyncio
async def test_newsapi_populates_published_at(patch_http):
    searcher = GenericAPISearcher("newsapi_org")
    results = await searcher.search("inflacao")
    assert results, "NewsAPI deve retornar resultados do fixture"
    assert all(r.source == "newsapi_org" for r in results)
    assert all(r.published_at is not None for r in results), (
        "Fase 1 exige published_at populado na NewsAPI"
    )
    assert results[0].metrics.get("source_name") == "Folha de S.Paulo"
    assert "folha.uol.com.br" in results[0].url


@pytest.mark.asyncio
async def test_newsapi_degrades_without_api_key(monkeypatch):
    """Sem NEWSAPI_KEY, a fonte degrada graciosamente (retorna [])."""
    import src.search.generic_api_searcher as mod

    monkeypatch.setattr(mod.os, "environ", {})
    monkeypatch.setattr(mod.GenericAPISearcher, "_http_request", _fake_api_request)
    searcher = GenericAPISearcher("newsapi_org")
    results = await searcher.search("inflacao")
    assert results == [], "NewsAPI sem chave deve retornar lista vazia"


# ───────────────────────── Bluesky ─────────────────────────
@pytest.mark.asyncio
async def test_bluesky_returns_results_with_source(patch_http):
    searcher = GenericAPISearcher("bluesky")
    results = await searcher.search("open source")
    assert results, "Bluesky deve retornar resultados do fixture"
    assert all(r.source == "bluesky" for r in results)
    assert results[0].published_at is not None
    assert "bsky.app/profile/technews.bsky.social/post/3k2abc123def456" in (
        results[0].url
    )
    assert results[0].metrics.get("author") == "Tech News BR"
    assert results[0].metrics.get("likes") == "215"


# ───────────────────────── Google News RSS ─────────────────────────
def _feed_xml() -> str:
    return (FIXTURES / "google_news_rss.xml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_google_news_rss_parses_and_populates_published_at(monkeypatch):
    import src.search.generic_feed_searcher as feed_mod

    searcher = GenericFeedSearcher("google_news_rss")
    monkeypatch.setattr(searcher, "_fetch", lambda url: _feed_xml())
    results = await searcher.search("inclusao digital")
    assert results, "Google News RSS deve retornar itens do feed"
    assert all(r.source == "google_news_rss" for r in results)
    assert all(r.published_at is not None for r in results), (
        "Fase 1 exige published_at populado no Google News RSS"
    )
    assert results[0].title == "Governo anuncia novo programa de inclusão digital"
    assert "news.google.com/rss/articles/article-1" in results[0].url
    # pubDate RFC822 -> datetime UTC-aware
    assert results[0].published_at.year == 2026
    assert results[0].published_at.month == 7


# ───────────────────────── parse_flexible_date ─────────────────────────
def test_parse_flexible_date_iso_and_rfc822():
    from datetime import timezone as dt_timezone

    iso = parse_flexible_date("2026-07-10T14:30:00Z")
    assert iso is not None and iso.tzinfo == dt_timezone.utc
    rfc = parse_flexible_date("Fri, 10 Jul 2026 14:30:00 GMT")
    assert rfc is not None and rfc.tzinfo == dt_timezone.utc
    assert parse_flexible_date(None) is None
    assert parse_flexible_date("") is None
    assert parse_flexible_date("not-a-date") is None


# ───────────────────────── Factory wiring ─────────────────────────
def test_factory_registers_news_sources():
    from src.search.factory import SearcherFactory

    known = SearcherFactory.get_available_searchers()
    for sid in ("gdelt", "newsapi_org", "bluesky", "mastodon_social", "google_news_rss"):
        assert sid in known, f"{sid} deve aparecer no roteamento do factory"
