"""Testes de fallback offline do SemanticReranker (F4 / C1).

Cobre o C1 do plano de blindagem black_ops: quando o download do peso HF
falha (sem rede / sem HF_TOKEN), o reranker deve usar reranking por score
local (_keyword_rerank) — sem crash e com log INFO. O score local deve
preservar o Top-10 por relevância de keyword-overlap.

O teste NÃO baixa modelo nem toca rede: força o cenário offline via env var
e/ou patch de _ensure_model.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from src.search.semantic_reranker import SemanticReranker


def _sample_results(n: int = 12):
    """Gera resultados fake com títulos de sobreposição variável com a query."""
    # Query alvo: "graph database performance benchmark"
    # Resultados com alta sobreposição vêm primeiro na ordem "certa".
    titles = [
        "Graph Database Performance Benchmark 2026",  # alta
        "Benchmark de Performance para Graph Databases",  # alta
        "Graph DB Latency Comparison",  # média
        "Database Performance Tuning Guide",  # média
        "Introduction to Graph Theory",  # baixa
        "Machine Learning Fundamentals",  # baixa
        "Cooking Recipes for Beginners",  # zero
        "Weather Forecast Weekly",  # zero
        "Sports News Today",  # zero
        "Travel Destinations 2026",  # zero
        "Random Offtopic Article",  # zero
        "Unrelated Content Here",  # zero
    ]
    return [
        {
            "title": titles[i],
            "url": f"http://example.com/{i}",
            "snippet": "",
            "content": "",
        }
        for i in range(min(n, len(titles)))
    ]


@pytest.mark.asyncio
async def test_hf_offline_detected_without_token(monkeypatch):
    """Sem HF_TOKEN/HF_HUB_OFFLINE, _hf_offline() deve retornar True."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    reranker = SemanticReranker()
    assert reranker._hf_offline() is True


@pytest.mark.asyncio
async def test_hf_online_with_token(monkeypatch):
    """Com HF_TOKEN presente, _hf_offline() deve retornar False (tenta baixar)."""
    monkeypatch.setenv("HF_TOKEN", "dummy-token")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    reranker = SemanticReranker()
    assert reranker._hf_offline() is False


@pytest.mark.asyncio
async def test_rerank_offline_falls_back_to_keyword_no_crash(monkeypatch):
    """Sem HF_TOKEN, rerank() usa score local e NÃO crasha."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    reranker = SemanticReranker()
    results = _sample_results(12)
    reranked = await reranker.rerank("graph database performance benchmark", results)

    # Não crashou e retornou todos os resultados.
    assert len(reranked) == 12
    # Modelo não foi carregado (cenário offline).
    assert reranker._model_available is False


@pytest.mark.asyncio
async def test_offline_rerank_preserves_top10_relevance(monkeypatch):
    """Score local deve colocar os 10 mais relevantes (por overlap) no topo."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    reranker = SemanticReranker()
    results = _sample_results(12)
    query = "graph database performance benchmark"
    reranked = await reranker.rerank(query, results, top_k=10)

    assert len(reranked) == 10
    # Os 2 primeiros devem ser os de alta sobreposição (graph + performance).
    top_titles = [r["title"] for r in reranked[:2]]
    assert any("Performance" in t for t in top_titles)
    assert any("Graph" in t for t in top_titles)
    # Nenhum resultado zero-overlap (receitas/clima/esportes) deve estar no top-10.
    zero_overlap = {"Cooking Recipes for Beginners", "Weather Forecast Weekly",
                    "Sports News Today", "Travel Destinations 2026",
                    "Random Offtopic Article", "Unrelated Content Here"}
    assert not (set(top_titles) & zero_overlap)


@pytest.mark.asyncio
async def test_ensure_model_handles_load_timeout(monkeypatch):
    """Timeout de download (asyncio.TimeoutError) → fallback offline, sem crash."""
    monkeypatch.setenv("HF_TOKEN", "dummy-token")  # simula ambiente "online"
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    reranker = SemanticReranker()

    # Força o _ensure_model a levantar TimeoutError logo no download.
    async def _fake_ensure():
        raise asyncio.TimeoutError()

    with patch.object(
        reranker, "_ensure_model", side_effect=_fake_ensure
    ):
        # O rerank deve capturar a indisponibilidade e usar keyword-fallback.
        results = _sample_results(5)
        reranked = await reranker.rerank("graph database benchmark", results)
        assert len(reranked) == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
