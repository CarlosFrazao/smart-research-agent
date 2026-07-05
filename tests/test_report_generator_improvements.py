"""
tests/test_report_generator_improvements.py - Testes de unidade para melhorias do ReportGenerator.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from src.report_generator import ReportGenerator
from src.types import ResearchMetadata, SynthesizedResult

@pytest.mark.asyncio
async def test_is_query_english():
    llm = MagicMock()
    gen = ReportGenerator(llm)

    assert gen._is_query_english("best Python framework vs Go") is True
    assert gen._is_query_english("how to build a client server api") is True
    assert gen._is_query_english("melhor framework de python") is False
    assert gen._is_query_english("qual biblioteca usar para orm") is False


@pytest.mark.asyncio
async def test_generate_empty_results():
    llm = MagicMock()
    gen = ReportGenerator(llm)
    metadata = ResearchMetadata(
        query="como fazer bolo",
        timestamp=datetime.now(),
        sources=["github"],
        total_results=0,
        iterations=1,
        duration_seconds=1.5,
        overall_confidence=0.5,
        low_confidence_warnings=[]
    )

    # Query em Português
    report_pt = await gen.generate("como fazer bolo", [], metadata)
    assert "Nenhum resultado relevante foi encontrado" in report_pt

    # Query em Inglês
    metadata.query = "how to bake cake"
    report_en = await gen.generate("how to bake cake", [], metadata)
    assert "No relevant results were found" in report_en


@pytest.mark.asyncio
async def test_save_report_slugification():
    llm = MagicMock()
    gen = ReportGenerator(llm)

    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        # Query com caracteres muito especiais proibidos no Windows
        query = "What is the best ORM for python/node.js? (Part 1) *cool*"
        md_path = gen.save_report("Test Content", query, output_dir=tmpdir)

        # O nome do arquivo deve ser sanitizado e seguro
        basename = os.path.basename(md_path)
        assert "/" not in basename
        assert "?" not in basename
        assert "*" not in basename
        assert "(" not in basename
        assert ")" not in basename
        assert "what-is-the-best-orm-for-python-node-js-part-1-coo" in basename

        # Teste de query vazia/símbolos que geraria slug vazio
        query_empty = "??? !!!"
        md_path_empty = gen.save_report("Test Content", query_empty, output_dir=tmpdir)
        basename_empty = os.path.basename(md_path_empty)
        assert "report" in basename_empty


@pytest.mark.asyncio
async def test_url_deduplication_and_headers_localization():
    llm = MagicMock()
    # Mock do generate_structured do LLM para evitar chamadas reais
    llm.generate_structured = AsyncMock(return_value={
        "executive_summary": "English summary of findings.",
        "recommendation": "English recommendation main: tool A, alt: tool B, steps: test it.",
        "trends": "English trend 1, trend 2."
    })

    gen = ReportGenerator(llm)

    results = [
        SynthesizedResult(
            entity="Tool A",
            title="Tool A",
            url="https://tool-a.com",
            urls=["https://tool-a.com", "https://duplicate.com", "https://tool-a.com"],
            sources=["github"],
            combined_score=95,
            description="Tool A description",
            highlights=["fast", "clean"],
            metrics={"stars": 1200}
        ),
        SynthesizedResult(
            entity="Tool B",
            title="Tool B",
            url="https://tool-b.com",
            urls=["https://tool-b.com", "https://duplicate.com"],
            sources=["github"],
            combined_score=85,
            description="Tool B description",
            highlights=["light"],
            metrics={"stars": 500}
        )
    ]

    metadata = ResearchMetadata(
        query="best python tools vs javascript",
        timestamp=datetime.now(),
        sources=["github"],
        total_results=2,
        iterations=1,
        duration_seconds=2.0,
        overall_confidence=0.9,
        low_confidence_warnings=[]
    )

    # Query em inglês
    report = await gen.generate("best python tools vs javascript", results, metadata)

    # 1. Verificar títulos estruturais em inglês
    assert "## 1. Executive Summary" in report
    assert "## 2. Discovered Projects / Tools" in report
    assert "## 3. Side-by-Side Comparison" in report
    assert "## 4. Identified Technologies / Stacks" in report
    assert "## 8. Links and References" in report

    # 2. Verificar a deduplicação de URLs (https://duplicate.com deve aparecer apenas uma vez nas referências)
    references_section = report.split("## 8. Links and References")[1]
    assert references_section.count("[https://duplicate.com]") == 1


def test_config_improvements():
    from src.config import Config
    import os
    from pathlib import Path

    # Testa se o placeholder do Firecrawl é tratado como None
    cfg = Config(firecrawl_api_key="fc-placeholder")
    assert cfg.firecrawl_api_key is None

    cfg2 = Config(firecrawl_api_key="fc_placeholder")
    assert cfg2.firecrawl_api_key is None

    # Testa se chaves válidas são mantidas
    cfg3 = Config(firecrawl_api_key="fc-real-key")
    assert cfg3.firecrawl_api_key == "fc-real-key"

    # Testa se o memory_db_path relativo é resolvido para um caminho absoluto
    cfg4 = Config(memory_db_path="reports/.research_memory.db")
    path = Path(cfg4.memory_db_path)
    assert path.is_absolute()
    assert "reports" in cfg4.memory_db_path
    assert ".research_memory.db" in cfg4.memory_db_path
