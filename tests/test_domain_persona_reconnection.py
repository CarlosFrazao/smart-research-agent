"""Testes de reconexão de formatação acadêmica (Fase 6.5).

Valida que ReportGenerator usa DomainPersona para montar a seção
de referências (## 9. Referências) conforme o domínio:
  - ai_ml / dev_tools / open_source -> IEEE
  - saas_b2b / general          -> APA
A seção é anexada a `_assemble_report` e respeita PT-BR/EN.
"""

import pytest
from datetime import datetime

from src.types import ResearchMetadata, SynthesizedResult
from src.report_generator import ReportGenerator


def _gen() -> ReportGenerator:
    # ReportGenerator sem LLM (testamos só a montagem de referências).
    return ReportGenerator.__new__(ReportGenerator)


def _result() -> SynthesizedResult:
    return SynthesizedResult(
        entity="x",
        title="LangChain: Building Apps",
        description="A framework.",
        sources=["github"],
        urls=["https://github.com/langchain-ai/langchain"],
        combined_score=90,
        metrics={"stars": 90000},
        first_seen=datetime(2023, 1, 1),
        last_seen=datetime(2024, 5, 1),
    )


def _meta(domain: str) -> ResearchMetadata:
    return ResearchMetadata(
        query="q",
        domain=domain,
        sources=["github"],
        total_results=1,
        iterations=1,
        duration_seconds=1.0,
        timestamp=datetime.now(),
    )


def _section_style(section: list[str]) -> str:
    # Linha de nota: "_Formatado segundo a norma X ..." / "_Formatted per X ..."
    for line in section:
        if "norma" in line or "citation style" in line:
            if "IEEE" in line:
                return "IEEE"
            if "Bluebook" in line:
                return "Bluebook"
            if "APA" in line:
                return "APA"
    return "UNKNOWN"


@pytest.mark.parametrize(
    "domain,expected_style",
    [
        ("ai_ml", "IEEE"),
        ("dev_tools", "IEEE"),
        ("open_source", "IEEE"),
        ("saas_b2b", "APA"),
        ("general", "APA"),
    ],
)
def test_domain_maps_to_style(domain, expected_style):
    gen = _gen()
    section = gen._build_references([_result()], _meta(domain), is_english=False)
    assert _section_style(section) == expected_style


def test_references_section_has_header_pt():
    gen = _gen()
    section = gen._build_references([_result()], _meta("ai_ml"), is_english=False)
    assert any(line.strip().startswith("## 9. Refer") for line in section)
    assert len(section) > 3  # ---, header, note, citation


def test_references_section_has_header_en():
    gen = _gen()
    section = gen._build_references([_result()], _meta("ai_ml"), is_english=True)
    assert any(line.strip().startswith("## 9. References") for line in section)


def test_ieee_citation_format():
    gen = _gen()
    section = gen._build_references([_result()], _meta("ai_ml"), is_english=False)
    citation = [l for l in section if l.strip().startswith("[1]")][0]
    # IEEE: [1] Autor, "Título", FONTE, Ano.
    assert citation.startswith("[1]")
    assert '"LangChain: Building Apps"' in citation
    assert "2024" in citation


def test_empty_results_yields_no_section():
    gen = _gen()
    section = gen._build_references([], _meta("general"), is_english=False)
    assert section == []


def test_assemble_report_includes_references():
    """A montagem completa do relatório inclui a seção de referências."""
    gen = _gen()
    from src.types import ReportFormat

    report = gen._assemble_report(
        query="q",
        metadata=_meta("ai_ml"),
        results=[_result()],
        executive_summary="resumo",
        recommendation="reco",
        trends="tend",
    )
    assert "## 9. Refer" in report
