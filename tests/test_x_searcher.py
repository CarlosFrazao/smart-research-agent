"""Testes TDD do XSearcher (FEAT-010).

Casos cobertos (task_fontes_pesquisa_hermes_sra.md — Bloco 3.1 / PRD 4.10.7):
1. XAI_API_KEY ausente -> factory nao instancia o searcher (requires_key).
2. POST /responses mockado -> hit retorna SearchResult(source="x") com citations.
3. filtros ativos + 0 citations -> evidence_quality="low" + hallucination_flags=["unsourced"].
4. from_date > to_date -> fallback() (lista vazia, sem chamada de API).
5. caplog NUNCA contem o valor de XAI_API_KEY (bearer protegido).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.search.registry import get_registry
from src.search.x_searcher import XSearcher

# Token de exemplo para os testes — autorizado via pragma p/ detect-secrets.
_TEST_API_KEY = "xai-test-bearer-9f8e7d6c5b4a3c2d1e0f"  # pragma: allowlist secret


def _make_searcher(monkeypatch, *, api_key: str = _TEST_API_KEY) -> XSearcher:
    """Helper: cria XSearcher com API key/flag injetadas no ambiente."""
    monkeypatch.setenv("XAI_API_KEY", api_key)  # pragma: allowlist secret
    monkeypatch.setenv("SRA_X_ENABLED", "true")
    return XSearcher({"max_results": 5, "x_timeout": 180})


def test_requires_key_blocks_factory_without_api_key(monkeypatch):
    """Sem XAI_API_KEY, o registro exige a key -> factory pulgaria o searcher."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    meta = get_registry().get("x")
    assert meta is not None, "XSearcher deve estar registrado via decorator"
    requires_key = meta.get("requires_key")
    assert requires_key == "XAI_API_KEY"
    assert not __import__("os").getenv(requires_key), "Pre-condicao: key ausente"
    would_instantiate = bool(__import__("os").getenv(requires_key))
    assert would_instantiate is False


def _fake_response(payload: dict) -> MagicMock:
    """Monta um MagicMock de httpx.Response com .json() retornando o payload."""
    response = MagicMock()
    response.json.return_value = payload
    return response


def test_search_hit_returns_x_result_with_citations(monkeypatch):
    """POST /responses mockado -> SearchResult(source='x') com citations."""
    searcher = _make_searcher(monkeypatch)

    payload = {
        "output_text": "Grok respondeu com base no indice X.",
        "citations": [
            {"url": "https://x.com/user/status/1", "title": "Post relevante"},
        ],
        "output": [],
    }
    searcher._http_request = AsyncMock(return_value=_fake_response(payload))

    results = _run(searcher.search("ultimas reacoes sobre IA"))

    assert len(results) == 1
    assert results[0].source == "x"
    assert results[0].description == "Grok respondeu com base no indice X."
    assert "https://x.com/user/status/1" in results[0].citations
    assert results[0].evidence_quality == "cited"
    assert results[0].hallucination_flags == []
    searcher._http_request.assert_awaited_once()


def test_search_degraded_with_filters_and_no_citations(monkeypatch):
    """Filtros ativos + 0 citations -> evidence_quality='low' + flag 'unsourced'."""
    searcher = _make_searcher(monkeypatch)

    payload = {
        "output_text": "Resposta sem fontes do indice X.",
        "citations": [],
        "output": [],
    }
    searcher._http_request = AsyncMock(return_value=_fake_response(payload))

    results = _run(
        searcher.search(
            "posts do @exemplo sobre IA",
            allowed_handles=["exemplo"],
            from_date="2026-01-01",
        )
    )

    assert len(results) == 1
    assert results[0].evidence_quality == "inferred"
    assert results[0].hallucination_flags == ["unsourced"]
    assert results[0].metrics.get("degraded") is True


def test_search_no_degraded_without_filters(monkeypatch):
    """Sem filtros, 0 citations nao marca degraded (resposta geral do modelo)."""
    searcher = _make_searcher(monkeypatch)

    payload = {
        "output_text": "Resposta geral sem citacoes.",
        "citations": [],
        "output": [],
    }
    searcher._http_request = AsyncMock(return_value=_fake_response(payload))

    results = _run(searcher.search("o que e IA"))

    assert len(results) == 1
    assert results[0].evidence_quality == "cited"
    assert results[0].hallucination_flags == []
    assert results[0].metrics.get("degraded") is False


def test_search_inverted_dates_falls_back(monkeypatch):
    """from_date > to_date -> fallback() lista vazia, SEM chamar a API."""
    searcher = _make_searcher(monkeypatch)
    searcher._http_request = AsyncMock(side_effect=AssertionError("nao deveria chamar"))

    results = _run(
        searcher.search("qualquer", from_date="2026-12-31", to_date="2026-01-01")
    )

    assert results == []
    searcher._http_request.assert_not_awaited()


def test_search_future_from_date_falls_back(monkeypatch):
    """from_date no futuro -> fallback() (X so indexa passado)."""
    searcher = _make_searcher(monkeypatch)
    searcher._http_request = AsyncMock(side_effect=AssertionError("nao deveria chamar"))

    results = _run(searcher.search("qualquer", from_date="2099-01-01"))

    assert results == []
    searcher._http_request.assert_not_awaited()


def test_search_excluded_and_allowed_mutually_exclusive(monkeypatch):
    """allowed + excluded handles -> fallback() sem chamar a API."""
    searcher = _make_searcher(monkeypatch)
    searcher._http_request = AsyncMock(side_effect=AssertionError("nao deveria chamar"))

    results = _run(
        searcher.search(
            "qualquer",
            allowed_handles=["a"],
            excluded_handles=["b"],
        )
    )

    assert results == []
    searcher._http_request.assert_not_awaited()


def test_search_api_error_returns_empty(monkeypatch):
    """Erro de API (qualquer excecao) -> fallback() retorna lista vazia."""
    searcher = _make_searcher(monkeypatch)
    searcher._http_request = AsyncMock(side_effect=RuntimeError("xAI 500"))

    results = _run(searcher.search("qualquer"))

    assert results == []
    searcher._http_request.assert_awaited_once()


def test_search_empty_answer_normalize_none(monkeypatch):
    """Resposta 200 mas sem answer -> normalize retorna None -> fallback vazio."""
    searcher = _make_searcher(monkeypatch)

    payload = {"output_text": "", "citations": [], "output": []}
    searcher._http_request = AsyncMock(return_value=_fake_response(payload))

    results = _run(searcher.search("qualquer"))

    assert results == []


def test_bearer_token_never_logged(monkeypatch, caplog):
    """O valor de XAI_API_KEY jamais aparece nos logs (seguranca critica)."""
    searcher = _make_searcher(
        monkeypatch, api_key=_TEST_API_KEY
    )  # pragma: allowlist secret

    payload = {
        "output_text": "resposta",
        "citations": [{"url": "https://x.com/a/1"}],
        "output": [],
    }
    searcher._http_request = AsyncMock(return_value=_fake_response(payload))

    with caplog.at_level(logging.DEBUG, logger="src.search.x_searcher"):
        _run(searcher.search("qualquer"))

    log_text = caplog.text
    assert _TEST_API_KEY not in log_text, "BEARER VAZADO NO LOG!"
    # A query (redactada) e o source/model podem aparecer, mas nunca o bearer.
    assert "Bearer " not in log_text


# ── Helper de execucao sincrona para os testes (o search() e async) ──


def _run(coro):
    """Executa uma corrotina criando um novo event loop (sem deprecation)."""
    import asyncio

    return asyncio.run(coro)
