import pytest
from unittest.mock import AsyncMock, MagicMock
from src.orchestrator import Orchestrator
from src.config import Config
from src.types import SearchResult

@pytest.mark.asyncio
async def test_research_e2e_pipeline():
    config = Config(anthropic_api_key="test-key", max_iterations=1)
    orch = Orchestrator(config)

    # Mock LLM calls to avoid calling API
    orch.llm.generate = AsyncMock(return_value="Resumo executivo gerado com sucesso pelo E2E.")
    orch.llm.generate_structured = AsyncMock(side_effect=[
        # 1. Intent analyzer
        {
            "domain": "saas_b2b",
            "entities": ["AFFiNE", "Notion"],
            "intention": "discover",
            "urgency": "nao",
            "confidence": "alta"
        },
        # 2. Query expander
        {
            "queries": [
                {"query": "AFFiNE open source Notion alternative", "type": "qualificador", "priority": "alta", "rationale": "test"},
                {"query": "AFFiNE alternative", "type": "qualificador", "priority": "media", "rationale": "test"}
            ]
        },
        # 3. Gap detector (iter 1)
        {
            "is_complete": True,
            "missing_aspects": [],
            "new_queries": [],
            "confidence": "alta",
            "rationale": "Pesquisa E2E completa"
        }
    ])

    # Mock actual search results returned by parallel_search
    mock_results = [
        SearchResult(
            source="github",
            title="toeverything/AFFiNE",
            url="https://github.com/toeverything/AFFiNE",
            description="AFFiNE is a next-gen collaborative knowledge base.",
            metrics={"stars": 20000, "forks": 1500, "language": "TypeScript"},
        ),
        SearchResult(
            source="hackernews",
            title="AFFiNE: An open source alternative to Notion",
            url="https://news.ycombinator.com/item?id=32000",
            description="Discussion about AFFiNE on Hacker News.",
            metrics={"points": 350, "comments": 80},
        )
    ]
    orch._parallel_search = AsyncMock(return_value=mock_results)

    # Executa a pesquisa E2E
    report = await orch.research("AFFiNE open source Notion alternative")

    # Asserts
    assert report is not None
    assert "# Relatorio:" in report
    assert "## 1. Resumo Executivo" in report
    assert "toeverything/AFFiNE" in report or "Project" in report or "AFFiNE" in report
    assert len(report) > 500
