"""Testes do GenericAPISearcher (Auditoria Parte 2 — Fase 6, Tarefa 6.1).

Cobrem: carregamento do catálogo YAML, construção de URL/params, resolução de
campos aninhados via JMESPath, url_template com placeholders, headers com env
vars, resposta como lista raiz (result_path null) e tolerância a falhas.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.search.generic_api_searcher import (
    GenericAPISearcher,
    _resolve_field,
    _resolve_placeholders,
    list_enabled_generic_source_ids,
    list_generic_source_ids,
)


def test_catalog_lists_expected_sources():
    """O catálogo deve conter ao menos 4 fontes válidas."""
    ids = list_generic_source_ids()
    assert "open_library" in ids
    assert "doaj" in ids
    assert "osm_nominatim" in ids
    assert len(ids) >= 4


def test_enabled_sources_excludes_disabled():
    """Fontes com enabled:false (pypi, open_meteo) NÃO devem ser registradas.

    O SearcherFactory itera sobre list_enabled_generic_source_ids(), então
    elas não viram searchers ativos apesar de aparecerem no catálogo.
    """
    enabled = list_enabled_generic_source_ids()
    assert "wikipedia" in enabled
    assert "open_library" in enabled
    assert "npm_registry" in enabled
    assert "musicbrainz" in enabled
    assert "domain_whois" in enabled
    assert "pypi" not in enabled
    assert "open_meteo" not in enabled


def test_unknown_source_raises_value_error():
    """Fonte inexistente no catálogo deve levantar ValueError."""
    with pytest.raises(ValueError):
        GenericAPISearcher("fonte_que_nao_existe")


def test_resolve_field_supports_nested_dot_path():
    """_resolve_field deve resolver caminhos aninhados (bibjson.title)."""
    item = {"bibjson": {"title": "Meu Artigo"}}
    assert _resolve_field(item, "bibjson.title") == "Meu Artigo"


def test_resolve_field_missing_returns_empty():
    """Campo ausente resolve para string vazia (nunca None/erro)."""
    assert _resolve_field({"a": 1}, "b.c") == ""
    assert _resolve_field({"a": 1}, None) == ""


def test_resolve_placeholders_from_item_keys():
    """url_template substitui {chave} pelos valores do item."""
    item = {"lat": "-23.5", "lon": "-46.6"}
    tmpl = "https://osm.org/#map=15/{lat}/{lon}"
    assert _resolve_placeholders(tmpl, item) == "https://osm.org/#map=15/-23.5/-46.6"


@pytest.mark.asyncio
async def test_search_parses_result_path_and_fields():
    """search() extrai a lista via result_path e normaliza título/url/snippet."""
    searcher = GenericAPISearcher("open_library")

    mock_resp = MagicMock()
    mock_resp.json = MagicMock(
        return_value={
            "docs": [
                {
                    "title": "Clean Code",
                    "key": "/works/OL1W",
                    "subtitle": "A handbook of craftsmanship.",
                }
            ]
        }
    )
    searcher._http_request = AsyncMock(return_value=mock_resp)

    results = await searcher.search("clean code")

    assert len(results) == 1
    assert results[0].title == "Clean Code"
    assert results[0].url == "https://openlibrary.org/works/OL1W"
    assert results[0].description == "A handbook of craftsmanship."
    assert results[0].source == "open_library"


@pytest.mark.asyncio
async def test_search_handles_root_list_response():
    """Fonte com result_path null trata resposta como lista raiz (Nominatim)."""
    searcher = GenericAPISearcher("osm_nominatim")

    mock_resp = MagicMock()
    mock_resp.json = MagicMock(
        return_value=[
            {
                "display_name": "São Paulo, Brasil",
                "type": "city",
                "lat": "-23.5",
                "lon": "-46.6",
            }
        ]
    )
    searcher._http_request = AsyncMock(return_value=mock_resp)

    results = await searcher.search("sao paulo")

    assert len(results) == 1
    assert results[0].title == "São Paulo, Brasil"
    assert results[0].description == "city"
    assert "-23.5" in results[0].url


@pytest.mark.asyncio
async def test_search_returns_empty_on_http_failure():
    """Falha HTTP não propaga exceção — retorna lista vazia."""
    searcher = GenericAPISearcher("open_library")
    searcher._http_request = AsyncMock(side_effect=RuntimeError("boom"))

    results = await searcher.search("qualquer")
    assert results == []


@pytest.mark.asyncio
async def test_search_respects_max_results_override():
    """kwargs max_results limita a quantidade de resultados retornados."""
    searcher = GenericAPISearcher("open_library")
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(
        return_value={"docs": [{"title": f"T{i}", "key": f"/w/{i}"} for i in range(20)]}
    )
    searcher._http_request = AsyncMock(return_value=mock_resp)

    results = await searcher.search("x", max_results=3)
    assert len(results) == 3


def test_headers_resolve_env_vars(monkeypatch):
    """Headers com {ENV_VAR} são resolvidos de os.environ."""
    monkeypatch.setenv("CORE_API_KEY", "secret123")
    searcher = GenericAPISearcher("core_ac_uk")
    headers = searcher._build_headers({"Authorization": "Bearer {CORE_API_KEY}"})
    assert headers["Authorization"] == "Bearer secret123"
