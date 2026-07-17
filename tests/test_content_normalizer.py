"""
Tests for ContentNormalizer (FEAT-003).

Cobrem: normalize (HTML→texto limpo), summarize em modo cost_optimization
(não chama LLM), summarize com LLM mockado, e degradação quando o LLM falha.
"""

from __future__ import annotations

import types

import pytest

from src.search.content_normalizer import ContentNormalizer, _strip_html_tags


_HTML_SAMPLE = """
<html>
  <head><title>Test</title><style>.x{color:red}</style></head>
  <body>
    <script>console.log('secret');</script>
    <h1>Headline Here</h1>
    <p>First paragraph with <b>bold</b> text and a   spaced   gap.</p>
    <!-- a comment -->
    <p>Second paragraph.</p>


    <p>Trailing boilerplate.</p>
  </body>
</html>
"""


def _make_fake_llm(raise_on_call: bool = False, summary: str = "RESUMOCURTO"):
    """Constrói um LLMClient falso com ``complete`` async.

    Args:
        raise_on_call: Se True, ``complete`` levanta exceção (simula falha).
        summary: Texto retornado pelo resumo quando não levanta.
    """

    async def _complete(prompt, task_type="synthesis", temperature=0.2, max_tokens=512):
        if raise_on_call:
            raise RuntimeError("LLM unavailable")
        return summary

    client = types.SimpleNamespace()
    client.complete = _complete
    return client


# ── normalize ────────────────────────────────────────────────────────────────


def test_normalize_strips_html_and_tags():
    norm = ContentNormalizer()
    out = norm.normalize(_HTML_SAMPLE)
    assert "<html>" not in out
    assert "<script>" not in out
    assert "console.log" not in out
    assert "Headline Here" in out
    assert "First paragraph" in out
    # Whitespace colapsado: não há sequências de espaços múltiplos.
    assert "   spaced   gap" not in out


def test_normalize_empty_returns_empty():
    norm = ContentNormalizer()
    assert norm.normalize("") == ""
    assert norm.normalize(None) == ""  # type: ignore[arg-type]


def test_normalize_redacts_secrets():
    norm = ContentNormalizer()
    raw = "Veja a chave sk-ant-abcdefghij1234567890XYZ dentro do texto."
    out = norm.normalize(raw)
    # A chave crua não deve aparecer; redact_sensitive_text mascara com
    # head/tail (sk-ant...0XYZ), preservando o prefixo visível e ofuscando o meio.
    assert "sk-ant-abcdefghij1234567890XYZ" not in out  # pragma: allowlist secret
    assert "sk-ant...0XYZ" in out


def test_normalize_unescapes_entities():
    norm = ContentNormalizer()
    out = norm.normalize("<p>joao &amp; maria &lt;3</p>")
    assert "joao & maria <3" == out


# ── summarize ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_cost_optimization_does_not_call_llm():
    calls = {"n": 0}

    async def _spy(*args, **kwargs):
        calls["n"] += 1
        return "NUNCADEVE"

    fake = types.SimpleNamespace()
    fake.complete = _spy
    norm = ContentNormalizer(llm_client=fake)
    out = await norm.summarize("texto grande " * 5000, max_tokens=100, cost_optimization=True)
    assert calls["n"] == 0
    assert "NUNCADEVE" not in out
    # Modo economia trunca e preserva conteúdo inicial.
    assert out.startswith("texto grande")


@pytest.mark.asyncio
async def test_summarize_without_llm_returns_truncated():
    norm = ContentNormalizer(llm_client=None)
    out = await norm.summarize("conteudo " * 4000, max_tokens=100)
    assert "CONTEÚDO TRUNCADO" in out
    assert "conteudo" in out


@pytest.mark.asyncio
async def test_summarize_calls_llm_and_returns_summary():
    fake = _make_fake_llm(summary="RESUMOCURTO")
    norm = ContentNormalizer(llm_client=fake)
    out = await norm.summarize("texto base " * 50, max_tokens=64)
    assert out == "RESUMOCURTO"


@pytest.mark.asyncio
async def test_summarize_llm_failure_degrades_to_truncated():
    fake = _make_fake_llm(raise_on_call=True)
    norm = ContentNormalizer(llm_client=fake)
    out = await norm.summarize("importante " * 4000, max_tokens=64)
    assert "CONTEÚDO TRUNCADO" in out
    assert "importante" in out


@pytest.mark.asyncio
async def test_summarize_empty_text_returns_empty():
    fake = _make_fake_llm()
    norm = ContentNormalizer(llm_client=fake)
    assert await norm.summarize("", max_tokens=64) == ""


@pytest.mark.asyncio
async def test_summarize_redacts_output():
    fake = _make_fake_llm(summary="chave sk-ant-abcdefghij1234567890XYZ visivel")
    norm = ContentNormalizer(llm_client=fake)
    out = await norm.summarize("texto base " * 50, max_tokens=128)
    assert "sk-ant-abcdefghij1234567890XYZ" not in out  # pragma: allowlist secret
    assert "sk-ant...0XYZ" in out


def test_strip_html_tags_removes_scripts():
    out = _strip_html_tags("<script>alert(1)</script><p>ok</p>")
    assert "alert(1)" not in out
    assert "ok" in out
