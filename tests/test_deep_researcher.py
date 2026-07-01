import pytest
from unittest.mock import AsyncMock, MagicMock
from src.deep_researcher import DeepResearcher, ResearchNode, DeepResearchResult
from src.types import SearchResult


def _make_result(url: str = "https://example.com", score: float = 0.7) -> SearchResult:
    r = SearchResult(
        source="web",
        title=f"Result for {url}",
        url=url,
        description="word " * 80,
        metrics={},
    )
    r.confidence_score = score
    return r


def _make_llm(hypotheses: list | None = None) -> MagicMock:
    llm = MagicMock()
    hyps = hypotheses or [
        "hypothesis A",
        "hypothesis B",
        "hypothesis C",
        "hypothesis D",
    ]
    llm.generate_structured = AsyncMock(return_value=hyps)
    return llm


# ─── ResearchNode ────────────────────────────────────────────────────────────

def test_research_node_defaults():
    node = ResearchNode(id="n1", query="test", hypothesis="test hyp")
    assert node.status == "pending"
    assert node.confidence == 0.0
    assert node.depth == 0
    assert node.children == []
    assert node.results == []


def test_research_node_with_children():
    child = ResearchNode(id="c1", query="child", hypothesis="child hyp", depth=1)
    parent = ResearchNode(id="p1", query="parent", hypothesis="parent hyp", children=[child])
    assert len(parent.children) == 1
    assert parent.children[0].depth == 1


# ─── DeepResearchResult ──────────────────────────────────────────────────────

def test_deep_research_result_fields():
    result = DeepResearchResult(
        findings=[_make_result()],
        reasoning_tree="## Reasoning Tree\n",
        total_nodes_explored=5,
        confirmed_hypotheses=["hyp A"],
        dead_end_hypotheses=["hyp B"],
    )
    assert result.total_nodes_explored == 5
    assert "## Reasoning Tree" in result.reasoning_tree
    assert len(result.findings) == 1


# ─── _estimate_confidence ────────────────────────────────────────────────────

def test_estimate_confidence_empty_results():
    dr = DeepResearcher(llm_client=_make_llm())
    assert dr._estimate_confidence([]) == 0.0


def test_estimate_confidence_averages_scores():
    dr = DeepResearcher(llm_client=_make_llm())
    results = [_make_result(score=0.8), _make_result(score=0.6)]
    assert dr._estimate_confidence(results) == pytest.approx(0.7, abs=0.01)


def test_estimate_confidence_capped_at_1():
    dr = DeepResearcher(llm_client=_make_llm())
    results = [_make_result(score=1.0), _make_result(score=1.0)]
    assert dr._estimate_confidence(results) <= 1.0


# ─── _generate_hypotheses ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_hypotheses_uses_llm():
    llm = _make_llm(["sub-query A", "sub-query B", "sub-query C", "sub-query D"])
    dr = DeepResearcher(llm_client=llm)
    hyps = await dr._generate_hypotheses("Python ORM", [])
    assert len(hyps) > 0
    assert all(isinstance(h, str) for h in hyps)


@pytest.mark.asyncio
async def test_generate_hypotheses_fallback_on_llm_error():
    llm = MagicMock()
    llm.generate_structured = AsyncMock(side_effect=Exception("LLM down"))
    dr = DeepResearcher(llm_client=llm)
    hyps = await dr._generate_hypotheses("Python ORM", [])
    assert len(hyps) == 4
    assert any("best practices" in h for h in hyps)


@pytest.mark.asyncio
async def test_generate_hypotheses_respects_max_branches():
    llm = _make_llm(["h1", "h2", "h3", "h4", "h5", "h6"])
    dr = DeepResearcher(llm_client=llm)
    hyps = await dr._generate_hypotheses("query", [])
    assert len(hyps) <= dr.MAX_BRANCHES


# ─── _explore_node: status transitions ───────────────────────────────────────

@pytest.mark.asyncio
async def test_explore_node_confirms_when_high_confidence():
    """Node with high-confidence results is marked confirmed without branching."""
    llm = _make_llm()
    dr = DeepResearcher(llm_client=llm)
    dr._search_for_node = AsyncMock(
        return_value=[_make_result(score=0.9), _make_result(score=0.9)]
    )

    node = ResearchNode(id="n1", query="test", hypothesis="hyp", depth=0)
    result = await dr._explore_node(node)

    assert result.status == "confirmed"
    assert result.confidence >= dr.CONFIRMED_THRESHOLD
    assert result.children == []


@pytest.mark.asyncio
async def test_explore_node_marks_dead_end_on_low_confidence_deep():
    """Node deeper than root with low confidence is pruned as dead_end."""
    llm = _make_llm()
    dr = DeepResearcher(llm_client=llm)
    dr._search_for_node = AsyncMock(return_value=[_make_result(score=0.1)])

    node = ResearchNode(id="n1", query="test", hypothesis="hyp", depth=1)
    result = await dr._explore_node(node)

    assert result.status == "dead_end"


@pytest.mark.asyncio
async def test_explore_node_stops_at_max_depth():
    """Node at MAX_DEPTH is marked explored even with medium confidence."""
    llm = _make_llm()
    dr = DeepResearcher(llm_client=llm)
    dr._search_for_node = AsyncMock(return_value=[_make_result(score=0.5)])

    node = ResearchNode(id="n1", query="test", hypothesis="hyp", depth=dr.MAX_DEPTH)
    result = await dr._explore_node(node)

    assert result.status == "explored"
    assert result.children == []


@pytest.mark.asyncio
async def test_explore_node_spawns_children_at_medium_confidence():
    """Node with medium confidence at depth 0 generates child branches."""
    llm = _make_llm(["h1", "h2"])
    dr = DeepResearcher(llm_client=llm)
    dr._search_for_node = AsyncMock(return_value=[_make_result(score=0.5)])

    node = ResearchNode(id="root", query="test", hypothesis="root", depth=0)
    result = await dr._explore_node(node)

    assert len(result.children) > 0


# ─── _consolidate_tree ───────────────────────────────────────────────────────

def test_consolidate_tree_skips_dead_ends():
    dead = ResearchNode(id="d1", query="dead", hypothesis="dead", status="dead_end")
    dead.results = [_make_result("https://dead.com")]

    confirmed = ResearchNode(id="c1", query="ok", hypothesis="ok", status="confirmed")
    confirmed.results = [_make_result("https://good.com", score=0.8)]

    root = ResearchNode(id="root", query="root", hypothesis="root", status="explored")
    root.children = [dead, confirmed]

    dr = DeepResearcher(llm_client=_make_llm())
    findings = dr._consolidate_tree(root)

    urls = [r.url for r in findings]
    assert "https://good.com" in urls
    assert "https://dead.com" not in urls


def test_consolidate_tree_deduplicates_by_url():
    result_a = _make_result("https://same.com", score=0.9)
    result_b = _make_result("https://same.com", score=0.7)

    node1 = ResearchNode(id="n1", query="q", hypothesis="h", status="confirmed")
    node1.results = [result_a]
    node2 = ResearchNode(id="n2", query="q", hypothesis="h", status="confirmed")
    node2.results = [result_b]

    root = ResearchNode(id="root", query="root", hypothesis="root", status="explored")
    root.children = [node1, node2]

    dr = DeepResearcher(llm_client=_make_llm())
    findings = dr._consolidate_tree(root)

    same_url = [r for r in findings if r.url == "https://same.com"]
    assert len(same_url) == 1


def test_consolidate_tree_sorted_by_confidence():
    low = _make_result("https://low.com", score=0.3)
    high = _make_result("https://high.com", score=0.9)

    root = ResearchNode(id="root", query="root", hypothesis="root", status="confirmed")
    root.results = [low, high]

    dr = DeepResearcher(llm_client=_make_llm())
    findings = dr._consolidate_tree(root)

    assert findings[0].url == "https://high.com"


# ─── _export_tree_as_markdown ────────────────────────────────────────────────

def test_export_tree_contains_reasoning_tree_heading():
    root = ResearchNode(id="root", query="Python ORMs", hypothesis="Python ORMs", status="confirmed")
    dr = DeepResearcher(llm_client=_make_llm())
    md = dr._export_tree_as_markdown(root)
    assert "## Reasoning Tree" in md


def test_export_tree_shows_status_icons():
    root = ResearchNode(id="root", query="q", hypothesis="root q", status="confirmed")
    child_dead = ResearchNode(id="c1", query="q1", hypothesis="dead hyp", status="dead_end", depth=1)
    child_exp = ResearchNode(id="c2", query="q2", hypothesis="explored hyp", status="explored", depth=1)
    root.children = [child_dead, child_exp]

    dr = DeepResearcher(llm_client=_make_llm())
    md = dr._export_tree_as_markdown(root)

    assert "✅" in md
    assert "❌" in md
    assert "🔍" in md


def test_export_tree_includes_root_query():
    root = ResearchNode(id="root", query="best Python ORMs 2026", hypothesis="best Python ORMs 2026", status="explored")
    dr = DeepResearcher(llm_client=_make_llm())
    md = dr._export_tree_as_markdown(root)
    assert "best Python ORMs 2026" in md


# ─── _collect_by_status ──────────────────────────────────────────────────────

def test_collect_by_status_confirmed():
    confirmed_child = ResearchNode(id="c1", query="q", hypothesis="confirmed hyp", status="confirmed")
    dead_child = ResearchNode(id="c2", query="q", hypothesis="dead hyp", status="dead_end")
    root = ResearchNode(id="root", query="root", hypothesis="root", status="explored")
    root.children = [confirmed_child, dead_child]

    dr = DeepResearcher(llm_client=_make_llm())
    confirmed = dr._collect_by_status(root, "confirmed")
    dead = dr._collect_by_status(root, "dead_end")

    assert "confirmed hyp" in confirmed
    assert "dead hyp" in dead


# ─── _count_nodes ────────────────────────────────────────────────────────────

def test_count_nodes_single():
    root = ResearchNode(id="root", query="q", hypothesis="h")
    dr = DeepResearcher(llm_client=_make_llm())
    assert dr._count_nodes(root) == 1


def test_count_nodes_with_children():
    root = ResearchNode(id="root", query="q", hypothesis="h")
    root.children = [
        ResearchNode(id="c1", query="q1", hypothesis="h1"),
        ResearchNode(id="c2", query="q2", hypothesis="h2"),
    ]
    root.children[0].children = [ResearchNode(id="gc1", query="q3", hypothesis="h3")]
    dr = DeepResearcher(llm_client=_make_llm())
    assert dr._count_nodes(root) == 4


# ─── research() integration (no orchestrator) ────────────────────────────────

@pytest.mark.asyncio
async def test_research_returns_deep_research_result():
    """Full research() call without orchestrator returns a valid result."""
    llm = _make_llm(["h1 query", "h2 query"])
    dr = DeepResearcher(llm_client=llm)

    result = await dr.research("Python async frameworks")

    assert isinstance(result, DeepResearchResult)
    assert isinstance(result.reasoning_tree, str)
    assert "## Reasoning Tree" in result.reasoning_tree
    assert result.total_nodes_explored >= 1


@pytest.mark.asyncio
async def test_research_reasoning_tree_in_output():
    """research() output contains the ## Reasoning Tree section."""
    llm = _make_llm(["hypothesis alpha", "hypothesis beta"])
    dr = DeepResearcher(llm_client=llm)

    result = await dr.research("test query for tree")

    assert "## Reasoning Tree" in result.reasoning_tree
