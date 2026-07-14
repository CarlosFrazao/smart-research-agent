"""Testes do CrossRefSearcher — busca de citações acadêmicas via CrossRef REST API.

Mantém o mesmo padrão de mock do test_pubmed_searcher.py (MagicMock(spec=HTTPClient)
com `.get` substituído por AsyncMock). O CrossRef retorna JSON puro no corpo da
resposta (`{"message": {"items": [...]}}`), que é exatamente o que o HTTPClient
atual devolve de `resp.json()` — portanto o mock injeta o dict direto, sem o
wrapper legado `{"json": ...}` usado por searchers mais antigos.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.search.crossref_searcher import CrossRefSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

# ── Fixtures de resposta CrossRef (formato real da API) ────────────────────────

_CROSSREF_OK = {
    "message": {
        "items": [
            {
                "DOI": "10.1000/crossref-001",
                "title": ["Concurrency Control in Distributed Systems"],
                "author": [
                    {"given": "Jane", "family": "Doe"},
                    {"given": "John", "family": "Smith"},
                ],
                "issued": {"date-parts": [[2023, 5, 14]]},
                "is-referenced-by-count": 128,
                "URL": "https://doi.org/10.1000/crossref-001",
                "type": "journal-article",
            },
            {
                "DOI": "10.1000/crossref-002",
                "title": ["A Survey of Lock-Free Data Structures"],
                "author": [{"given": "Alice", "family": "Nakamura"}],
                "issued": {"date-parts": [[2021]]},
                "is-referenced-by-count": 12,
                "URL": "https://doi.org/10.1000/crossref-002",
                "type": "proceedings-article",
            },
        ]
    }
}

_CROSSREF_EMPTY = {"message": {"items": []}}


def _make_searcher(config: dict | None = None) -> CrossRefSearcher:
    """Instancia o CrossRefSearcher com config mínima."""
    return CrossRefSearcher(config or {"timeout": 10, "max_results": 10})


def _mock_http(searcher: CrossRefSearcher, payload: dict) -> MagicMock:
    """Substitui o HTTPClient do searcher por um mock que retorna ``payload``."""
    http_mock = MagicMock(spec=HTTPClient)
    http_mock.get = AsyncMock(return_value=payload)
    searcher.http = http_mock
    return http_mock


@pytest.mark.asyncio
async def test_crossref_search_success():
    """Busca bem-sucedida normaliza título, DOI, autores, citações e data."""
    searcher = _make_searcher()
    _mock_http(searcher, _CROSSREF_OK)

    results = await searcher.search("distributed concurrency control")

    assert len(results) == 2

    r1 = results[0]
    assert r1.source == "crossref"
    assert r1.title == "Concurrency Control in Distributed Systems"
    assert r1.metrics["doi"] == "10.1000/crossref-001"
    assert r1.url == "https://doi.org/10.1000/crossref-001"
    assert r1.metrics["citations"] == 128
    # published_at derivado de date-parts [[2023, 5, 14]]
    assert r1.published_at is not None
    assert r1.published_at.year == 2023
    assert r1.published_at.month == 5
    # Descrição expõe autores e DOI para rastreabilidade
    assert "Jane Doe, John Smith" in r1.description
    assert "10.1000/crossref-001" in r1.description

    r2 = results[1]
    assert r2.metrics["doi"] == "10.1000/crossref-002"
    # date-parts com apenas o ano → dia/mês default preservado
    assert r2.published_at is not None
    assert r2.published_at.year == 2021


@pytest.mark.asyncio
async def test_crossref_normalize_handles_missing_fields():
    """Itens sem data-parts/autores não quebram o parse (published_at None)."""
    payload = {
        "message": {
            "items": [
                {
                    "DOI": "10.1000/crossref-minimal",
                    "title": ["Minimal Record"],
                    "URL": "https://doi.org/10.1000/crossref-minimal",
                }
            ]
        }
    }
    searcher = _make_searcher()
    _mock_http(searcher, payload)

    results = await searcher.search("minimal record")

    assert len(results) == 1
    r = results[0]
    assert r.source == "crossref"
    assert r.title == "Minimal Record"
    assert r.published_at is None  # sem issued.date-parts
    assert r.metrics["citations"] == 0  # is-referenced-by-count ausente


@pytest.mark.asyncio
async def test_crossref_web_fallback_on_empty():
    """Sem resultados nativos, dispara o WebSearcher fallback (padrão GAP1)."""
    searcher = _make_searcher()
    _mock_http(searcher, _CROSSREF_EMPTY)

    web_mock = MagicMock()
    web_mock.enabled = True
    web_mock.search = AsyncMock(
        return_value=[
            SearchResult(
                source="web",
                title="Web CrossRef Fallback",
                url="http://web.example/article",
                description="Fallback description",
            )
        ]
    )
    searcher.web_fallback = web_mock

    results = await searcher.search("obscure citation topic")

    assert len(results) == 1
    assert results[0].source == "web"
    assert results[0].title == "Web CrossRef Fallback"
    web_mock.search.assert_called_once()


@pytest.mark.asyncio
async def test_crossref_polite_user_agent_header():
    """A requisição envia User-Agent polite exigido pela CrossRef."""
    searcher = _make_searcher()
    http_mock = _mock_http(searcher, _CROSSREF_OK)

    await searcher.search("polite agent check")

    # http.get foi chamado com headers contendo o User-Agent polite
    assert http_mock.get.await_count >= 1
    call_kwargs = http_mock.get.call_args.kwargs
    headers = call_kwargs.get("headers", {})
    assert "User-Agent" in headers
    assert "mailto:" in headers["User-Agent"]


def test_crossref_registered_in_factory():
    """SearcherFactory registra o 'crossref' de forma always-on."""
    from src.search.factory import SearcherFactory

    orchestrator = MagicMock()
    orchestrator.config.timeout_per_source = 30
    orchestrator.config.max_results_per_source = 10
    orchestrator.config.github_token = None
    orchestrator.config.producthunt_token = None
    orchestrator.config.firecrawl_api_key = None
    orchestrator.config.firecrawl_base_url = None
    orchestrator.config.spider_api_key = None
    orchestrator.config.spider_base_url = None
    orchestrator.config.steel_api_key = None
    orchestrator.config.steel_base_url = None

    searchers = SearcherFactory.create_searchers(orchestrator)

    assert "crossref" in searchers
    assert isinstance(searchers["crossref"], CrossRefSearcher)


def test_crossref_in_available_searchers():
    """get_available_searchers() reconhece 'crossref' para roteamento."""
    from src.search.factory import SearcherFactory

    assert "crossref" in SearcherFactory.get_available_searchers()
