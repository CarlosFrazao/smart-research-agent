import asyncio
import pytest
from src.mcp_server import create_app
from src.hitl_manager import HITLManager
from src.orchestrator import Orchestrator
from src.types import IntentResult, Domain, Intention, ExpandedQuery


@pytest.mark.asyncio
async def test_orchestrator_hitl_integration(monkeypatch):
    # Mocking IntentAnalyzer at module level
    async def mock_analyze(*args, **kwargs):
        return IntentResult(
            domain=Domain.SAAS_B2B,
            intention=Intention.COMPARE,
            urgency="nao",
            confidence="alta",
            entities=[],
        )

    monkeypatch.setattr("src.intent_analyzer.IntentAnalyzer.analyze", mock_analyze)

    # Mocking QueryExpander at module level
    async def mock_expand(*args, **kwargs):
        return [
            ExpandedQuery(
                query="competidor a", type="similar", priority="alta", rationale=""
            ),
            ExpandedQuery(
                query="competidor b", type="similar", priority="alta", rationale=""
            ),
        ]

    monkeypatch.setattr("src.query_expander.QueryExpander.expand", mock_expand)

    # Initialize app and orchestrator
    app = create_app()
    container = app.state.container
    orc: Orchestrator = container.resolve("orchestrator")
    hitl: HITLManager = container.resolve("hitl_manager")

    # Disable actual search stage logic for testing to avoid network requests
    search_stage = orc._pipeline.stages[2]  # "search" stage

    async def mock_search_run(context):
        return context

    search_stage.run = mock_search_run

    # Short-circuit all subsequent stages to avoid LLM / network calls
    for stage in orc._pipeline.stages[3:]:

        async def mock_stage_run(context, s=stage):
            return context

        stage.run = mock_stage_run

    session_id = "test_integration_session"

    # Start research in background
    task = asyncio.create_task(orc.research("test query", session_id=session_id))

    # Allow it to run up to the pause in ExpandStage
    await asyncio.sleep(0.5)

    # Verify that the session is registered as pending in HITLManager
    pending = hitl.get_pending_request(session_id)
    assert pending is not None
    assert pending["request_type"] == "source_plan"
    assert len(pending["data"]["queries"]) == 3

    # User modifies/approves the queries and plan
    user_approved_data = {
        "queries": [
            {
                "query": "competidor a",
                "type": "similar",
                "priority": "alta",
                "rationale": "",
            },
            {
                "query": "competidor c",
                "type": "user_approved",
                "priority": "alta",
                "rationale": "",
            },
        ],
        "source_plan": pending["data"]["source_plan"],
    }

    # Submit response to resume execution
    success = await hitl.submit_response(session_id, user_approved_data)
    assert success is True

    # Wait for research to finish
    await task

    # Confirm the session was cleaned up
    assert hitl.get_pending_request(session_id) is None
