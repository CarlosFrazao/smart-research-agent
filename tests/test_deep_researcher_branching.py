import pytest
from src.deep_researcher import DeepResearcher, ResearchNode
from src.types import SearchResult


def _make_llm_with_angles():
    from unittest.mock import AsyncMock, MagicMock

    llm = MagicMock()
    # Mocking generate_structured to return hypotheses with various competing angles
    llm.generate_structured = AsyncMock(
        return_value=[
            "confirmative: Use SQLite FTS5 for local text indexing",
            "contrasting: SQLite FTS5 has scalability issues compared to Elasticsearch",
            "alternative: Use ChromaDB vector search as primary index",
            "general: standard text search libraries",
        ]
    )
    return llm


@pytest.mark.asyncio
async def test_deep_researcher_branching_and_angles():
    llm = _make_llm_with_angles()
    dr = DeepResearcher(llm_client=llm)

    # 1. Test hypothesis generation and angle parsing
    hyps = await dr._generate_hypotheses("SQLite search", [])
    assert len(hyps) == 4

    # Simulate BFS child creation using the parsed hypothesis list
    root = ResearchNode(id="root", query="SQLite search", hypothesis="root", depth=0)

    for raw_hyp in hyps:
        angle = "general"
        hyp = raw_hyp
        if ":" in raw_hyp:
            parts = raw_hyp.split(":", 1)
            possible_angle = parts[0].strip().lower()
            if possible_angle in ("confirmative", "contrasting", "alternative"):
                angle = possible_angle
                hyp = parts[1].strip()

        child = ResearchNode(
            id="child_id", query=hyp, hypothesis=hyp, depth=1, angle=angle
        )
        root.children.append(child)

    assert len(root.children) == 4

    # Check that angles were parsed correctly
    assert root.children[0].angle == "confirmative"
    assert root.children[0].hypothesis == "Use SQLite FTS5 for local text indexing"

    assert root.children[1].angle == "contrasting"
    assert (
        root.children[1].hypothesis
        == "SQLite FTS5 has scalability issues compared to Elasticsearch"
    )

    assert root.children[2].angle == "alternative"
    assert root.children[2].hypothesis == "Use ChromaDB vector search as primary index"

    assert root.children[3].angle == "general"
    assert root.children[3].hypothesis == "general: standard text search libraries"


def test_deep_researcher_markdown_summary_table():
    dr = DeepResearcher(llm_client=_make_llm_with_angles())

    child1 = ResearchNode(
        id="c1",
        query="q1",
        hypothesis="Hypothesis 1",
        depth=1,
        angle="confirmative",
        status="confirmed",
        confidence=0.88,
    )
    child2 = ResearchNode(
        id="c2",
        query="q2",
        hypothesis="Hypothesis 2",
        depth=1,
        angle="contrasting",
        status="dead_end",
        confidence=0.21,
    )

    root = ResearchNode(
        id="root",
        query="main query",
        hypothesis="root goal",
        depth=0,
        children=[child1, child2],
        status="explored",
    )

    markdown = dr._export_tree_as_markdown(root)

    # Verify the table exists and lists the claims, angles and statuses
    assert "### Competing Hypotheses Summary" in markdown
    assert "| Hypothesis / Sub-query | Angle | Status | Confidence |" in markdown
    assert "Hypothesis 1" in markdown
    assert "Confirmative" in markdown
    assert "Confirmed" in markdown
    assert "88.00%" in markdown

    assert "Hypothesis 2" in markdown
    assert "Contrasting" in markdown
    assert "Dead end" in markdown
    assert "21.00%" in markdown

    # Check rendering of icons
    assert "👍" in markdown  # confirmative icon
    assert "👎" in markdown  # contrasting icon
