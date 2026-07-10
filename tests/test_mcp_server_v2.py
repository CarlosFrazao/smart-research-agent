import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.types import SearchResult


# ─── helpers ────────────────────────────────────────────────────────────────


def _make_search_result(**kwargs) -> SearchResult:
    defaults = dict(
        title="Test Result",
        url="https://github.com/test/repo",
        description="A test description with enough content to pass scoring.",
        source="github",
        confidence_score=0.75,
        evidence_quality="verified",
        hallucination_flags=[],
        contradictions=[],
        citations=[],
    )
    defaults.update(kwargs)
    return SearchResult(**defaults)


def _make_deep_result():
    finding = _make_search_result(
        title="Deep Research Report",
        description="Content here.",
        confidence_score=0.82,
    )
    result = MagicMock()
    result.findings = [finding]
    result.reasoning_tree = "## Reasoning Tree\n\n### Root"
    result.confirmed_hypotheses = ["hyp1", "hyp2", "hyp3"]
    result.dead_end_hypotheses = ["dead1"]
    result.total_nodes_explored = 5
    return result


# ─── lazy import of mcp_server (avoids real Orchestrator init) ───────────────


@pytest.fixture(autouse=True)
def reset_globals():
    import src.mcp_server as srv

    srv._orchestrator = None
    srv._deep_researcher = None
    srv._confidence_scorer = None
    yield
    srv._orchestrator = None
    srv._deep_researcher = None
    srv._confidence_scorer = None


# ─── get_orchestrator / get_deep_researcher / get_confidence_scorer ──────────


def test_get_orchestrator_lazy_init():
    import src.mcp_server as srv
    import src.orchestrator_factory as factory_mod

    # get_orchestrator agora delega para create_orchestrator (§14.2), que
    # seleciona Orchestrator ou ReActOrchestrator conforme a config.
    mock_orc = MagicMock()
    orig_factory = factory_mod.create_orchestrator
    factory_mod.create_orchestrator = MagicMock(return_value=mock_orc)
    try:
        orc = srv.get_orchestrator()
    finally:
        factory_mod.create_orchestrator = orig_factory
    assert orc is mock_orc


def test_get_orchestrator_singleton():
    import src.mcp_server as srv
    import src.orchestrator_factory as factory_mod

    mock_orc = MagicMock()
    orig_factory = factory_mod.create_orchestrator
    mock_factory = MagicMock(return_value=mock_orc)
    factory_mod.create_orchestrator = mock_factory
    try:
        srv.get_orchestrator()
        srv.get_orchestrator()
    finally:
        factory_mod.create_orchestrator = orig_factory
    mock_factory.assert_called_once()


def test_get_deep_researcher_lazy_init():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_dr = MagicMock()
    srv._orchestrator = mock_orc
    orig_dr = srv.DeepResearcher
    srv.DeepResearcher = MagicMock(return_value=mock_dr)
    try:
        dr = srv.get_deep_researcher()
    finally:
        srv.DeepResearcher = orig_dr
    assert dr is mock_dr


def test_get_confidence_scorer_lazy_init():
    import src.mcp_server as srv

    mock_cs = MagicMock()
    orig_cs = srv.ConfidenceScorer
    srv.ConfidenceScorer = MagicMock(return_value=mock_cs)
    try:
        cs = srv.get_confidence_scorer()
    finally:
        srv.ConfidenceScorer = orig_cs
    assert cs is mock_cs


def test_get_confidence_scorer_singleton():
    import src.mcp_server as srv

    mock_cs = MagicMock()
    orig_cs = srv.ConfidenceScorer
    mock_factory = MagicMock(return_value=mock_cs)
    srv.ConfidenceScorer = mock_factory
    try:
        srv.get_confidence_scorer()
        srv.get_confidence_scorer()
    finally:
        srv.ConfidenceScorer = orig_cs
    mock_factory.assert_called_once()


# ─── research_technology_v2 — standard mode ──────────────────────────────────


@pytest.mark.asyncio
async def test_research_v2_standard_mode_calls_orchestrator():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc.research = AsyncMock(return_value="# Report standard")
    srv._orchestrator = mock_orc

    result = await srv.research_technology_v2("test query", mode="standard")

    assert result == "# Report standard"
    mock_orc.research.assert_called_once_with("test query")


@pytest.mark.asyncio
async def test_research_v2_deep_mode_calls_deep_researcher():
    import src.mcp_server as srv

    mock_dr = MagicMock()
    mock_dr.research = AsyncMock(return_value=_make_deep_result())
    srv._deep_researcher = mock_dr
    srv._orchestrator = MagicMock()

    result = await srv.research_technology_v2("deep query", mode="deep")

    assert "Deep Research Report" in result
    mock_dr.research.assert_called_once_with("deep query")


@pytest.mark.asyncio
async def test_research_v2_deep_mode_includes_confidence_summary():
    import src.mcp_server as srv

    mock_dr = MagicMock()
    mock_dr.research = AsyncMock(return_value=_make_deep_result())
    srv._deep_researcher = mock_dr
    srv._orchestrator = MagicMock()

    result = await srv.research_technology_v2(
        "query", mode="deep", include_confidence=True
    )

    assert "Confidence Summary" in result
    assert (
        "82%" in result
    )  # overall_confidence calculado a partir de findings[0].confidence_score=0.82
    assert "Reasoning Tree" in result


@pytest.mark.asyncio
async def test_research_v2_deep_mode_no_confidence_when_disabled():
    import src.mcp_server as srv

    mock_dr = MagicMock()
    mock_dr.research = AsyncMock(return_value=_make_deep_result())
    srv._deep_researcher = mock_dr
    srv._orchestrator = MagicMock()

    result = await srv.research_technology_v2(
        "query", mode="deep", include_confidence=False
    )

    assert "Confidence Summary" not in result


@pytest.mark.asyncio
async def test_research_v2_returns_error_string_on_exception():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc.research = AsyncMock(side_effect=RuntimeError("boom"))
    srv._orchestrator = mock_orc

    result = await srv.research_technology_v2("query", mode="standard")

    assert "Erro" in result
    assert "boom" in result


# ─── scrape_url ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_url_returns_json_with_content():
    import src.mcp_server as srv

    sr = _make_search_result(description="Scraped markdown content")
    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[sr])
    mock_cs = MagicMock()
    mock_cs.score_result = AsyncMock(return_value=sr)
    srv._orchestrator = mock_orc
    srv._confidence_scorer = mock_cs

    raw = await srv.scrape_url("https://example.com")
    data = json.loads(raw)

    assert data["url"] == "https://example.com"
    assert data["content"] == "Scraped markdown content"
    assert "confidence_score" in data
    assert "evidence_quality" in data


@pytest.mark.asyncio
async def test_scrape_url_empty_result_returns_none_scraper():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[])
    srv._orchestrator = mock_orc
    srv._confidence_scorer = MagicMock()

    raw = await srv.scrape_url("https://empty.com")
    data = json.loads(raw)

    assert data["scraper_used"] == "none"
    assert data["content"] == ""


@pytest.mark.asyncio
async def test_scrape_url_returns_error_json_on_exception():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(side_effect=RuntimeError("timeout"))
    srv._orchestrator = mock_orc
    srv._confidence_scorer = MagicMock()

    raw = await srv.scrape_url("https://broken.com")
    data = json.loads(raw)

    assert "error" in data
    assert "timeout" in data["error"]


# ─── confidence_check ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confidence_check_returns_json_structure():
    import src.mcp_server as srv

    sr = _make_search_result(confidence_score=0.8)
    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[sr])
    mock_cs = MagicMock()
    mock_cs.score_result = AsyncMock(return_value=sr)
    srv._orchestrator = mock_orc
    srv._confidence_scorer = mock_cs

    raw = await srv.confidence_check(
        "FastAPI is fast", ["https://github.com/tiangolo/fastapi"]
    )
    data = json.loads(raw)

    assert data["claim"] == "FastAPI is fast"
    assert "overall_confidence" in data
    assert "evidence_quality" in data
    assert "recommendation" in data
    assert "supporting_sources" in data
    assert "contradicting_sources" in data


@pytest.mark.asyncio
async def test_confidence_check_high_score_recommends_use():
    import src.mcp_server as srv

    sr = _make_search_result(confidence_score=0.85, evidence_quality="verified")
    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[sr])
    mock_cs = MagicMock()
    mock_cs.score_result = AsyncMock(return_value=sr)
    srv._orchestrator = mock_orc
    srv._confidence_scorer = mock_cs

    raw = await srv.confidence_check("claim", ["https://verified-source.com"])
    data = json.loads(raw)

    assert data["recommendation"] == "use_with_confidence"


@pytest.mark.asyncio
async def test_confidence_check_low_score_recommends_do_not_use():
    import src.mcp_server as srv

    sr = _make_search_result(confidence_score=0.2, evidence_quality="unknown")
    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[sr])
    mock_cs = MagicMock()
    mock_cs.score_result = AsyncMock(return_value=sr)
    srv._orchestrator = mock_orc
    srv._confidence_scorer = mock_cs

    raw = await srv.confidence_check("dubious claim", ["https://untrusted.biz"])
    data = json.loads(raw)

    assert data["recommendation"] == "do_not_use"


@pytest.mark.asyncio
async def test_confidence_check_no_sources_accessible():
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[])
    srv._orchestrator = mock_orc
    srv._confidence_scorer = MagicMock()

    raw = await srv.confidence_check("claim", ["https://unreachable.xyz"])
    data = json.loads(raw)

    assert data["overall_confidence"] == 0.0
    assert "no_sources_accessible" in data["hallucination_flags"]
    assert data["recommendation"] == "do_not_use"


@pytest.mark.asyncio
async def test_confidence_check_caps_sources_at_five():
    import src.mcp_server as srv

    sr = _make_search_result(confidence_score=0.6)
    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(return_value=[sr])
    mock_cs = MagicMock()
    mock_cs.score_result = AsyncMock(return_value=sr)
    srv._orchestrator = mock_orc
    srv._confidence_scorer = mock_cs

    sources = [f"https://s{i}.com" for i in range(10)]
    raw = await srv.confidence_check("many sources", sources)
    data = json.loads(raw)

    assert data["sources_checked"] <= 5


@pytest.mark.asyncio
async def test_confidence_check_source_failure_treated_as_no_sources():
    """When all source fetches fail (warning path), returns no_sources_accessible."""
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc._select_scraper_for_url = AsyncMock(
        side_effect=RuntimeError("network fail")
    )
    srv._orchestrator = mock_orc
    srv._confidence_scorer = MagicMock()

    raw = await srv.confidence_check("claim", ["https://broken.com"])
    data = json.loads(raw)

    assert data["overall_confidence"] == 0.0
    assert "no_sources_accessible" in data["hallucination_flags"]


# ─── existing tools still present ────────────────────────────────────────────


def test_mcp_server_module_imports_without_error():
    import src.mcp_server as srv

    assert srv.app is not None


def test_health_endpoint_exists():
    from fastapi.testclient import TestClient
    import src.mcp_server as srv

    client = TestClient(srv.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ─── §15.2 — Unificação dos servidores FastAPI ───────────────────────────────


def test_rest_router_mounted_under_api_v2():
    """As rotas exclusivas de api/main.py devem responder sob /api/v2.

    Nota: nesta versão do FastAPI, ``include_router`` não achata as rotas em
    ``app.routes`` (usa um wrapper ``_IncludedRouter``), então validamos via
    TestClient — uma rota montada responde algo diferente de 404.
    """
    from fastapi.testclient import TestClient
    import src.mcp_server as srv

    client = TestClient(srv.app, raise_server_exceptions=False)

    # circuit-breakers não exige corpo nem auth: se montada, responde 200.
    # (Sem o include_router sob /api/v2, este caminho não existiria → 404.)
    assert client.get("/api/v2/api/circuit-breakers").status_code == 200
    # source-costs por sessão também é GET puro e prova a montagem do prefixo.
    assert client.get("/api/v2/api/source-costs/sessao-x").status_code == 200


def test_research_endpoint_returns_500_on_error():
    """§15.2 — erro no pipeline deve virar HTTP 500, não HTTP 200 com {'error'}."""
    from fastapi.testclient import TestClient
    import src.mcp_server as srv

    mock_orc = MagicMock()
    mock_orc.research = AsyncMock(side_effect=RuntimeError("boom"))
    srv._orchestrator = mock_orc

    client = TestClient(srv.app, raise_server_exceptions=False)
    response = client.post("/research", json={"query": "algo que falha"})
    assert response.status_code == 500
    assert "boom" in response.json()["detail"]


def test_research_endpoint_returns_400_on_missing_query():
    """Query vazia deve retornar HTTP 400, não HTTP 200 com {'error'}."""
    from fastapi.testclient import TestClient
    import src.mcp_server as srv

    client = TestClient(srv.app, raise_server_exceptions=False)
    response = client.post("/research", json={"query": ""})
    assert response.status_code == 400
