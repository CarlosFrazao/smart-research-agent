"""tests/test_stream_monitor_integration.py — Testes de integração do StreamMonitorAgent no Orquestrador, Pipeline e API FastAPI.
"""
from __future__ import annotations

import pytest

# Todos os testes neste módulo são de integração.
pytestmark = pytest.mark.integration

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Config
from src.orchestrator import Orchestrator
from src.pipeline.pipeline import PipelineContext
from src.pipeline.stages.search_stage import SearchStage
from src.stream_monitor_agent import StreamMonitorAgent
from src.types import SearchResult


@pytest.mark.asyncio
async def test_config_fields():
    config = Config(
        enable_live_monitoring=True,
        monitoring_feeds=[
            {
                "name": "HN",
                "url": "https://news.ycombinator.com/rss",
                "source_type": "rss",
                "topics": ["tech"],
            }
        ],
    )
    assert config.enable_live_monitoring is True
    assert len(config.monitoring_feeds) == 1
    assert config.monitoring_feeds[0]["name"] == "HN"


@pytest.mark.asyncio
async def test_orchestrator_initialization():
    config = Config(
        enable_live_monitoring=True,
        monitoring_feeds=[
            {
                "name": "HN",
                "url": "https://news.ycombinator.com/rss",
                "source_type": "rss",
                "topics": ["tech"],
            }
        ],
    )

    # Mock do component initialization para evitar conexões de banco de dados
    with patch("src.pipeline.stage_factory.StageFactory.initialize_components") as mock_init_comp, \
         patch("src.pipeline.stage_factory.StageFactory.build_pipeline") as mock_build_pipe, \
         patch("src.orchestrator.FallbackManager") as mock_fallback:

        orchestrator = Orchestrator(config=config)

        assert orchestrator.stream_monitor is not None
        assert len(orchestrator.stream_monitor._feeds) == 1
        assert any(f.name == "HN" for f in orchestrator.stream_monitor._feeds)


@pytest.mark.asyncio
async def test_orchestrator_lifecycle():
    config = Config(enable_live_monitoring=True)

    with patch("src.pipeline.stage_factory.StageFactory.initialize_components"), \
         patch("src.pipeline.stage_factory.StageFactory.build_pipeline"), \
         patch("src.orchestrator.FallbackManager"):

        orchestrator = Orchestrator(config=config)
        orchestrator.operation_mode = MagicMock()
        orchestrator.operation_mode.name = "cirurgia"
        orchestrator.operation_mode.enable_debate = False
        orchestrator.memory = None  # Evita AttributeError no close()

        mock_monitor = AsyncMock(spec=StreamMonitorAgent)
        mock_monitor._running = False
        orchestrator.stream_monitor = mock_monitor

        # Teste do start no research()
        progress_callback = MagicMock()
        # Mock do pipeline.run para evitar execução do pipeline real
        orchestrator._pipeline = AsyncMock()
        orchestrator._pipeline.run.return_value = MagicMock()
        orchestrator.close_searchers = AsyncMock()

        await orchestrator.research("query", progress_callback=progress_callback)
        mock_monitor.start.assert_called_once()

        # Teste do stop no close()
        mock_monitor._running = True
        await orchestrator.close()
        mock_monitor.stop.assert_called_once()


@pytest.mark.asyncio
async def test_search_stage_injection():
    # Setup context e mocks
    context = PipelineContext(query="test")
    context.source_plan = MagicMock()  # Satisfaz validação do SearchStage
    context.intent = MagicMock()       # Satisfaz validação do SearchStage
    orchestrator_mock = MagicMock()
    context.extras["orchestrator"] = orchestrator_mock

    # Mock do stream_monitor
    mock_monitor = AsyncMock()
    mock_monitor.events_as_search_results.return_value = [
        SearchResult(
            title="Evento do Monitor",
            url="http://example.com/1",
            snippet="Resumo do evento",
            source="rss",
            evidence_quality="cited",
        )
    ]
    orchestrator_mock.stream_monitor = mock_monitor

    # Instancia o stage de pesquisa com a assinatura real
    stage = SearchStage(
        searchers={},
        cache=MagicMock(),
        ranker=AsyncMock(),
        config=MagicMock(fallback_on_empty=False),
    )
    stage.ranker.rank = AsyncMock(return_value=[])

    # Força _build_tasks a retornar vazio
    stage._build_tasks = MagicMock()
    stage._build_tasks.return_value = asyncio.Future()
    stage._build_tasks.return_value.set_result(([], []))

    res_context = await stage.run(context)

    # Verifica que o evento foi injetado nos resultados
    assert len(res_context.raw_results) == 1
    assert res_context.raw_results[0].title == "Evento do Monitor"
    mock_monitor.events_as_search_results.assert_called_once_with(limit=10)


def test_api_endpoints():
    from src.mcp_server import app, get_orchestrator_dep

    # Mock do orquestrador com stream_monitor habilitado
    orchestrator_mock = MagicMock()
    mock_monitor = MagicMock()
    orchestrator_mock.stream_monitor = mock_monitor

    # Override do dependency injection no FastAPI
    app.dependency_overrides[get_orchestrator_dep] = lambda: orchestrator_mock
    client = TestClient(app)

    try:
        # 1. Test POST /api/v1/monitor/feeds
        mock_monitor.add_feed = MagicMock()
        payload = {
            "name": "HN Test",
            "url": "https://news.ycombinator.com/rss",
            "source_type": "rss",
            "topics": ["tech"],
            "poll_interval": 300,
        }
        response = client.post("/api/v1/monitor/feeds", json=payload)
        assert response.status_code == 201
        assert response.json() == {"status": "added", "feed": "HN Test"}
        mock_monitor.add_feed.assert_called_once()

        # 2. Test DELETE /api/v1/monitor/feeds/{name}
        mock_monitor.remove_feed.return_value = True
        response = client.delete("/api/v1/monitor/feeds/HN%20Test")
        assert response.status_code == 200
        assert response.json() == {"status": "removed", "feed": "HN Test"}
        mock_monitor.remove_feed.assert_called_once_with("HN Test")

        # DELETE inexistente
        mock_monitor.remove_feed.return_value = False
        response = client.delete("/api/v1/monitor/feeds/HN%20Inexistente")
        assert response.status_code == 404

        # 3. Test GET /api/v1/monitor/report
        report_mock = MagicMock()
        report_mock.model_dump.return_value = {"uptime": 100, "active_feeds": 1}
        mock_monitor.get_report.return_value = report_mock
        response = client.get("/api/v1/monitor/report")
        assert response.status_code == 200
        assert response.json() == {"uptime": 100, "active_feeds": 1}
        mock_monitor.get_report.assert_called_once()

    finally:
        app.dependency_overrides.clear()
