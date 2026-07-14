"""Testes de claim-level traceability no Synthesizer (Bloco 5 / E1-T1).

Cobrem o modelo `SynthesizedClaim` (Pydantic v2) e o método aditivo
`Synthesizer.synthesize_with_claims()`, garantindo:
  - claims não-vazios derivados da síntese;
  - cada claim carrega pelo menos um `source_id`;
  - `as_markdown()` devolve o texto puro (backward compat);
  - `as_cited_markdown()` insere referências inline `[[N]](url)`;
  - `synthesize()` legado permanece com o mesmo contrato
    (`list[SynthesizedResult]`) — nenhum breaking change.
"""

import pytest

from src.synthesizer import Synthesizer
from src.types import RankedResult, SynthesizedClaim, SynthesizedResult


def _ranked(source: str, title: str, url: str, score: float = 80.0) -> RankedResult:
    """Fabrica um RankedResult mínimo válido para os testes."""
    return RankedResult(
        source=source,
        title=title,
        url=url,
        description=f"Descrição detalhada sobre {title} com contexto suficiente.",
        score=score,
        metrics={},
    )


# ── Modelo SynthesizedClaim ────────────────────────────────────────────────


def test_claim_as_markdown_returns_plain_text() -> None:
    """`as_markdown()` retorna o texto puro, sem referências."""
    claim = SynthesizedClaim(
        text="O framework X é o mais adotado.",
        source_ids=["abc123"],
        urls=["https://example.com/x"],
    )
    assert claim.as_markdown() == "O framework X é o mais adotado."
    assert "[[" not in claim.as_markdown()


def test_claim_as_cited_markdown_inserts_references() -> None:
    """`as_cited_markdown()` anexa referências inline `[[N]](url)`."""
    claim = SynthesizedClaim(
        text="O framework X é o mais adotado.",
        source_ids=["abc123", "def456"],
        urls=["https://example.com/x", "https://example.com/y"],
    )
    cited = claim.as_cited_markdown()
    assert "[[1]](https://example.com/x)" in cited
    assert "[[2]](https://example.com/y)" in cited
    assert cited.startswith("O framework X é o mais adotado.")


def test_claim_as_cited_markdown_without_urls_is_plain() -> None:
    """Sem URLs, `as_cited_markdown()` degrada para o texto puro."""
    claim = SynthesizedClaim(text="Afirmação sem fonte rastreável.")
    assert claim.as_cited_markdown() == "Afirmação sem fonte rastreável."


def test_claim_confidence_bounds_enforced() -> None:
    """A confiança é validada no intervalo [0.0, 1.0] (Pydantic v2)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SynthesizedClaim(text="x", confidence=1.5)


# ── Synthesizer.synthesize_with_claims ─────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_with_claims_returns_non_empty_claims() -> None:
    """`synthesize_with_claims()` devolve uma lista de claims não-vazia."""
    synth = Synthesizer(llm_client=None)
    results = [
        _ranked("github", "Framework Alpha", "https://gh.com/alpha", 90.0),
        _ranked("reddit", "Ferramenta Beta", "https://rd.com/beta", 70.0),
    ]
    synthesized, claims = await synth.synthesize_with_claims(results)
    assert isinstance(synthesized, list)
    assert all(isinstance(s, SynthesizedResult) for s in synthesized)
    assert claims, "esperava ao menos um claim derivado"
    assert all(isinstance(c, SynthesizedClaim) for c in claims)


@pytest.mark.asyncio
async def test_each_claim_has_source_id() -> None:
    """Cada claim carrega pelo menos um `source_id` rastreável."""
    synth = Synthesizer(llm_client=None)
    results = [
        _ranked("github", "Projeto Gamma", "https://gh.com/gamma", 85.0),
    ]
    _, claims = await synth.synthesize_with_claims(results)
    assert claims
    for claim in claims:
        assert claim.source_ids, f"claim sem source_id: {claim.text!r}"


@pytest.mark.asyncio
async def test_claim_carries_url_and_cited_markdown_has_reference() -> None:
    """O claim propaga a URL da fonte e a versão citada expõe `[[1]](url)`."""
    synth = Synthesizer(llm_client=None)
    results = [
        _ranked("github", "Projeto Delta", "https://gh.com/delta", 88.0),
    ]
    _, claims = await synth.synthesize_with_claims(results)
    assert claims
    claim = claims[0]
    assert any("gh.com/delta" in u for u in claim.urls)
    assert "[[1]](https://gh.com/delta)" in claim.as_cited_markdown()


@pytest.mark.asyncio
async def test_synthesize_legacy_still_returns_result_list() -> None:
    """Regressão: `synthesize()` mantém o contrato `list[SynthesizedResult]`."""
    synth = Synthesizer(llm_client=None)
    results = [
        _ranked("github", "Projeto Epsilon", "https://gh.com/epsilon", 75.0),
    ]
    out = await synth.synthesize(results)
    assert isinstance(out, list)
    assert all(isinstance(s, SynthesizedResult) for s in out)


@pytest.mark.asyncio
async def test_synthesize_empty_input_returns_empty() -> None:
    """Entrada vazia produz síntese e claims vazios (sem exceção)."""
    synth = Synthesizer(llm_client=None)
    synthesized, claims = await synth.synthesize_with_claims([])
    assert synthesized == []
    assert claims == []
