import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────


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


@pytest.fixture
def mock_orchestrator():
    """Orchestrator que retorna um relatório estável para cada monitor rodado."""
    orch = MagicMock()
    orch.research = AsyncMock(
        return_value="# Relatório de Vigília\n\n## 1. Resumo\n\nNovidades sobre o tópico.\n---\n"
    )
    return orch


def _patch_orchestrator(mock_orchestrator):
    import src.mcp_server as srv

    srv._orchestrator = mock_orchestrator
    return srv


# ── TOOL 17 — monitor_topic: create ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_topic_create_adds_job(mock_orchestrator, tmp_path, monkeypatch):
    """action='create' registra um ScheduledJob em reports/monitors e retorna o id."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)
    # Garante que reports/monitors seja resolvido dentro de tmp_path.
    monkeypatch.chdir(tmp_path)

    raw = await srv.monitor_topic(action="create", topic="AI regulation", check_interval_minutes=120)
    data = json.loads(raw)

    assert data["status"] == "created"
    assert data["topic"] == "AI regulation"
    assert data["monitor_id"]
    assert data["cron"] == "0 */2 * * *"  # 120min -> 2h
    # Confirma que o job foi persistido no ResearchScheduler.
    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(orchestrator=mock_orchestrator)
    monitors = [j for j in scheduler._jobs.values() if j.output_dir == "reports/monitors"]
    assert len(monitors) == 1
    assert monitors[0].id == data["monitor_id"]


@pytest.mark.asyncio
async def test_monitor_topic_create_requires_topic(mock_orchestrator):
    """action='create' sem topic retorna erro estruturado."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)

    raw = await srv.monitor_topic(action="create", topic=None)
    data = json.loads(raw)
    assert "error" in data
    assert "topic" in data["error"]


# ── TOOL 17 — monitor_topic: list ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_topic_list_returns_only_monitors(mock_orchestrator, tmp_path, monkeypatch):
    """action='list' traz apenas jobs da pasta reports/monitors, ignorando outros."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)
    monkeypatch.chdir(tmp_path)

    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(orchestrator=mock_orchestrator)
    # Job de monitoramento (reports/monitors).
    scheduler.schedule_research("topic A", "0 9 * * *", "reports/monitors")
    # Job de outro tipo — não deve aparecer na listagem de monitores.
    scheduler.schedule_research("topic B", "0 9 * * *", "reports/scheduled")

    raw = await srv.monitor_topic(action="list")
    data = json.loads(raw)
    assert len(data["monitors"]) == 1
    assert data["monitors"][0]["topic"] == "topic A"
    assert data["monitors"][0]["monitor_id"]


# ── TOOL 17 — monitor_topic: check ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_topic_check_runs_job_and_returns_summary(mock_orchestrator, tmp_path, monkeypatch):
    """action='check' executa o job e retorna o sumário + mudanças detectadas."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)
    monkeypatch.chdir(tmp_path)

    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(orchestrator=mock_orchestrator)
    monitor_id = scheduler.schedule_research(
        "quantum computing", "0 9 * * *", "reports/monitors"
    )

    raw = await srv.monitor_topic(action="check", monitor_id=monitor_id)
    data = json.loads(raw)

    assert data["monitor_id"] == monitor_id
    assert data["topic"] == "quantum computing"
    assert "report_summary" in data
    assert "Relatório de Vigília" in data["report_summary"]
    # Sem relatório anterior => sem mudanças (lista vazia), mas campo presente.
    assert data["changes_detected"] == []
    mock_orchestrator.research.assert_called_once_with("quantum computing")


@pytest.mark.asyncio
async def test_monitor_topic_check_unknown_id_returns_error(mock_orchestrator):
    """action='check' com id inexistente retorna erro estruturado."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)

    raw = await srv.monitor_topic(action="check", monitor_id="does-not-exist")
    data = json.loads(raw)
    assert "error" in data


# ── TOOL 17 — monitor_topic: delete ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_topic_delete_removes_job(mock_orchestrator, tmp_path, monkeypatch):
    """action='delete' remove o job de monitoramento."""
    import src.mcp_server as srv

    _patch_orchestrator(mock_orchestrator)
    monkeypatch.chdir(tmp_path)

    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(orchestrator=mock_orchestrator)
    monitor_id = scheduler.schedule_research(
        "delete me", "0 9 * * *", "reports/monitors"
    )

    raw = await srv.monitor_topic(action="delete", monitor_id=monitor_id)
    data = json.loads(raw)
    assert data["deleted"] is True
    assert data["monitor_id"] == monitor_id

    scheduler2 = ResearchScheduler(orchestrator=mock_orchestrator)
    assert monitor_id not in scheduler2._jobs


# ── Endpoint REST — /api/v1/briefing/latest ──────────────────────────────────


def test_briefing_endpoint_aggregates_monitors(mock_orchestrator, tmp_path, monkeypatch):
    """O endpoint roda cada monitor e compila o relatório Markdown do briefing."""
    import src.mcp_server as srv

    monkeypatch.chdir(tmp_path)

    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(orchestrator=mock_orchestrator)
    scheduler.schedule_research("topic X", "0 9 * * *", "reports/monitors")
    scheduler.schedule_research("topic Y", "0 9 * * *", "reports/monitors")

    # App isolado (create_app) para não poluir o singleton srv.app (cujas rotas
    # /research dependem do container DI não-resolvido em outros testes). O
    # orchestrator do container resolve para o mock via global _orchestrator.
    srv._orchestrator = mock_orchestrator
    isolated_app = srv.create_app()

    # Isola a execução do job via patch do método de scheduler para não disparar
    # o pipeline de pesquisa completo dentro do teste.
    run_mock = AsyncMock(return_value="# Relatório topic\n\n## 1. Novidade\n\nTexto.\n---\n")
    monkeypatch.setattr(ResearchScheduler, "run_scheduled_research", run_mock)

    client = TestClient(isolated_app, raise_server_exceptions=False)
    response = client.get("/api/v1/briefing/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert set(body["monitors_checked"]) == {"topic X", "topic Y"}
    assert "Briefing Diário Automatizado" in body["briefing_md"]
    assert "topic X" in body["briefing_md"]
    assert "topic Y" in body["briefing_md"]
    # Cada monitor disparou uma execução de job.
    assert run_mock.call_count == 2


def test_briefing_endpoint_empty_when_no_monitors(mock_orchestrator, tmp_path, monkeypatch):
    """Sem monitores ativos, o endpoint retorna 200 com aviso amigável."""
    import src.mcp_server as srv

    monkeypatch.chdir(tmp_path)

    from src.scheduler import ResearchScheduler

    # Persiste estado vazio de monitores (apenas garante cwd isolado).
    ResearchScheduler(orchestrator=mock_orchestrator)

    srv._orchestrator = mock_orchestrator
    isolated_app = srv.create_app()

    client = TestClient(isolated_app, raise_server_exceptions=False)
    response = client.get("/api/v1/briefing/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["monitors_checked"] == []
    assert "monitor_topic" in body["briefing_md"]


# ── TOOL 18 — get_trending ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trending_parses_gdelt_response():
    """get_trending converte a resposta do GDELT em lista de tópicos normalizados."""
    import src.mcp_server as srv

    fake_payload = {
        "articles": [
            {
                "title": "Breaking: AI Act",
                "url": "https://example.com/ai",
                "domain": "example.com",
                "language": "English",
                "tone": "-1.2",
            },
            {
                "title": "Markets rise",
                "url": "https://news.test/markets",
                "domain": "news.test",
                "language": "English",
                "tone": "2.5",
            },
        ]
    }

    class _FakeResp:
        status_code = 200

        def json(self):
            return fake_payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResp()

    import httpx

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _FakeClient()
    try:
        raw = await srv.get_trending(hours=24, max_records=10)
    finally:
        httpx.AsyncClient = orig

    data = json.loads(raw)
    assert data["timeframe_hours"] == 24
    assert len(data["topics"]) == 2
    assert data["topics"][0]["title"] == "Breaking: AI Act"
    assert data["topics"][0]["url"] == "https://example.com/ai"
    assert data["topics"][0]["domain"] == "example.com"
    assert data["topics"][0]["tone"] == "-1.2"


@pytest.mark.asyncio
async def test_get_trending_clamps_max_records():
    """max_records acima de 20 é limitado a 20 na chamada GDELT."""
    import src.mcp_server as srv

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"articles": []}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            captured["url"] = url
            return _FakeResp()

    import httpx

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _FakeClient()
    try:
        raw = await srv.get_trending(hours=12, max_records=999)
    finally:
        httpx.AsyncClient = orig

    data = json.loads(raw)
    assert data["timeframe_hours"] == 12
    assert "maxrecords=20" in captured["url"]
    assert data["topics"] == []


@pytest.mark.asyncio
async def test_get_trending_handles_error_status():
    """Status != 200 do GDELT retorna erro estruturado, não quebra."""
    import src.mcp_server as srv

    class _FakeResp:
        status_code = 429

        def json(self):
            raise ValueError("no json")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResp()

    import httpx

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _FakeClient()
    try:
        raw = await srv.get_trending(hours=24, max_records=5)
    finally:
        httpx.AsyncClient = orig

    data = json.loads(raw)
    assert "error" in data
    assert "429" in data["error"]
