import pytest
from unittest.mock import AsyncMock, MagicMock
from src.storm_perspectives import StormPerspectiveGenerator


@pytest.mark.asyncio
async def test_storm_perspectives_llm_success():
    # Setup mock LLM
    llm = MagicMock()
    mock_perspectives = [
        {
            "name": "Cloud Engineer",
            "description": "Focuses on cost, memory and latency of operation.",
            "sub_queries": [
                "SQLite replication latency LiteFS",
                "SQLite AWS EBS performance",
            ],
        },
        {
            "name": "Database Administrator",
            "description": "Focuses on transaction isolation and backup scripts.",
            "sub_queries": [
                "SQLite write lock concurrency",
                "SQLite backup vacuum online",
            ],
        },
    ]
    llm.generate_structured = AsyncMock(return_value=mock_perspectives)

    generator = StormPerspectiveGenerator(llm_client=llm)
    res = await generator.generate_perspectives_with_queries(
        "SQLite in Enterprise SaaS", num_perspectives=2
    )

    assert len(res) == 2
    assert res[0]["name"] == "Cloud Engineer"
    assert "LiteFS" in res[0]["sub_queries"][0]
    assert res[1]["name"] == "Database Administrator"


@pytest.mark.asyncio
async def test_storm_perspectives_llm_fallback():
    # Setup mock LLM that raises Exception
    llm = MagicMock()
    llm.generate_structured = AsyncMock(side_effect=Exception("LLM Timeout"))

    generator = StormPerspectiveGenerator(llm_client=llm)
    res = await generator.generate_perspectives_with_queries(
        "Docker RAM Optimization", num_perspectives=3
    )

    assert len(res) == 3
    assert res[0]["name"] == "Technical Architect"
    assert res[1]["name"] == "Security & Compliance Auditor"
    assert res[2]["name"] == "Product & Business Strategist"

    # Check that topic was formatted into sub_queries
    assert "Docker RAM Optimization" in res[0]["sub_queries"][0]
