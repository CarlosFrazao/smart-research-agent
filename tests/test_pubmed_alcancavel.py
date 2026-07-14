"""Testes de alcançabilidade do PubMed via CLI (GAP 2 do PLANO_FECHAR_GAPS.md).

Hoje o PubMed é inalcançável via CLI porque nenhum OperationConfig lista
"pubmed" em `searchers` e nenhum domínio de `domains.yaml` o inclui. Como
`SearchService.execute` filtra o plano pelo `operation_mode.searchers`
(src/services/search_service.py:181), o PubMed só roda se (a) o SourcePlanner
o incluir no plano E (b) ele estiver na lista `searchers` do modo ativo.

Estes testes garantem ambos os lados.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.operation_modes import OperationModes

DOMAINS_YAML = Path(__file__).parent.parent / "config" / "domains.yaml"


@pytest.mark.asyncio
async def test_academico_mode_includes_pubmed():
    """O preset 'academico' deve listar pubmed em searchers."""
    mode = OperationModes.get_mode("academico")
    assert "pubmed" in mode.searchers
    assert "arxiv" in mode.searchers
    assert "semantic_scholar" in mode.searchers


@pytest.mark.asyncio
async def test_auto_select_routes_biomed_query_to_academico():
    """Query biomédica deve auto-selecionar o modo 'academico'."""
    assert OperationModes.auto_select("ensaios clínicos sobre covid") == "academico"
    assert OperationModes.auto_select("clinical trial diabetes treatment") == "academico"
    assert OperationModes.auto_select("pubmed biomarker study") == "academico"
    assert OperationModes.auto_select("artigo médico sobre hipertensão") == "academico"


@pytest.mark.asyncio
async def test_validate_operation_modes_accepts_academico():
    """O novo preset não deve quebrar a validação de modos."""
    # Não deve levantar
    OperationModes.validate_operation_modes()
    assert "academico" in OperationModes.MODES


@pytest.mark.asyncio
async def test_domains_yaml_lists_pubmed_for_academic_domains():
    """domains.yaml deve incluir pubmed em ai_ml e general (onde caem queries acadêmicas)."""
    assert DOMAINS_YAML.exists(), "config/domains.yaml ausente"
    with open(DOMAINS_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    domains = data.get("domains", {})
    assert "pubmed" in domains["ai_ml"]["secondary"]
    assert "pubmed" in domains["general"]["secondary"]


@pytest.mark.asyncio
async def test_pubmed_not_filtered_when_mode_is_academico():
    """SearchService.execute não descarta pubmed se o modo ativo é 'academico'.

    Espelha a lógica de filtro de src/services/search_service.py:181 sem
    precisar instanciar a rede/LLM.
    """
    mode = OperationModes.get_mode("academico")

    class _FakePlan:
        sources = {"pubmed": ["query"], "arxiv": ["query"]}

    included = [
        name
        for name in _FakePlan.sources
        if name in mode.searchers
    ]
    assert "pubmed" in included
    assert "arxiv" in included


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
