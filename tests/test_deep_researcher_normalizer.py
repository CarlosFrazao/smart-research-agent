"""Testes do FEAT-004 — DeepResearcher usa ContentNormalizer (economia de tokens).

Cobertura:
  1. Sem normalizador → fallback para texto bruto truncado (compatibilidade).
  2. Com normalizador offline (sem LLMClient) → descrições limpas/truncadas.
  3. Com normalizador + LLMClient mock → descrições resumidas via ``complete``.
  4. Economia de tokens registrada no TokenEconomy quando há redução real.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.deep_researcher import DeepResearcher
from src.search.content_normalizer import ContentNormalizer
from src.token_economy import TokenEconomy
from src.types import SearchResult


def _make_result(desc: str = "word " * 80, url: str = "https://example.com") -> SearchResult:
    r = SearchResult(
        source="web",
        title=f"Result for {url}",
        url=url,
        description=desc,
        metrics={},
    )
    r.confidence_score = 0.7
    return r


def _make_llm(hypotheses=None) -> MagicMock:
    llm = MagicMock()
    hyps = hypotheses or ["hyp A", "hyp B", "hyp C", "hyp D"]
    llm.generate_structured = AsyncMock(return_value=hyps)
    return llm


def _make_normalizer(llm_client=None) -> ContentNormalizer:
    return ContentNormalizer(llm_client=llm_client)


# ─── 1. Fallback sem normalizador (compatibilidade pré-FEAT-004) ─────────────


@pytest.mark.asyncio
async def test_no_normalizer_falls_back_to_raw_truncated():
    dr = DeepResearcher(llm_client=_make_llm())
    ctx = await dr._build_result_context([_make_result()])
    # Sem normalizador, usa texto bruto truncado a 80 chars.
    assert ctx.startswith("- Result for https://example.com:")
    assert len(ctx) <= 120  # título + ": " + ate 80 chars de descrição


@pytest.mark.asyncio
async def test_empty_results_empty_context():
    dr = DeepResearcher(llm_client=_make_llm())
    assert await dr._build_result_context([]) == ""


# ─── 2. Normalizador offline (sem LLMClient) ─────────────────────────────────


@pytest.mark.asyncio
async def test_offline_normalizer_cleans_description():
    dr = DeepResearcher(
        llm_client=_make_llm(),
        content_normalizer=_make_normalizer(llm_client=None),
    )
    raw_desc = "<p>  Heavy   HTML   with   whitespace  </p>" * 10
    ctx = await dr._build_result_context([_make_result(desc=raw_desc)])
    assert "<p>" not in ctx
    assert "Heavy HTML with whitespace" in ctx


# ─── 3. Normalizador com LLMClient (resumo via complete) ─────────────────────


@pytest.mark.asyncio
async def test_normalizer_summarizes_via_llm():
    llm_client = MagicMock()
    llm_client.complete = AsyncMock(return_value="RESUMO CURTO DO CONTEUDO")
    dr = DeepResearcher(
        llm_client=_make_llm(),
        content_normalizer=_make_normalizer(llm_client=llm_client),
    )
    ctx = await dr._build_result_context([_make_result(desc="x" * 5000)])
    assert "RESUMO CURTO DO CONTEUDO" in ctx
    # O resumo curto (21 chars) substitui a descrição bruta (5000 chars).
    assert "x" * 50 not in ctx


# ─── 4. Economia de tokens registrada ────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_economy_records_savings():
    token_economy = TokenEconomy()
    llm = _make_llm()
    llm.token_economy = token_economy
    llm_client = MagicMock()
    llm_client.complete = AsyncMock(return_value="resumo")
    dr = DeepResearcher(
        llm_client=llm,
        content_normalizer=_make_normalizer(llm_client=llm_client),
    )
    tokens_before = dr.budget.tokens_used
    await dr._build_result_context([_make_result(desc="y" * 4000)])
    # Houve redução real (4000 chars -> ~6 chars de resumo) → tokens contabilizados.
    assert dr.budget.tokens_used > tokens_before


@pytest.mark.asyncio
async def test_no_savings_when_normalizer_missing():
    token_economy = TokenEconomy()
    llm = _make_llm()
    llm.token_economy = token_economy
    dr = DeepResearcher(llm_client=llm, content_normalizer=None)
    tokens_before = dr.budget.tokens_used
    await dr._build_result_context([_make_result(desc="y" * 4000)])
    # Sem normalizador não há economia a registrar.
    assert dr.budget.tokens_used == tokens_before
