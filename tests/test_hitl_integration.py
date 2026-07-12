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

    # Short-circuit todas as stages EXCETO 'intent' e 'expand'. Substituir por
    # índice é frágil — o pipeline ganhou a stage 'storm' e outras, deslocando
    # as posições. Fazemos por NOME.
    #   - 'intent' precisa rodar (com IntentAnalyzer.analyze mockado) porque a
    #     pausa HITL de 'source_plan' no ExpandStage só ocorre quando
    #     context.intent está preenchido.
    #   - 'expand' é onde a pausa HITL de fato acontece.
    live_stages = {"intent", "expand"}
    for stage in orc._pipeline.stages:
        if stage.name in live_stages:
            continue

        async def mock_stage_run(context, s=stage):
            return context

        stage.run = mock_stage_run

    session_id = "test_integration_session"

    # Query específica o bastante para NÃO acionar a detecção de "query vaga"
    # (FASE 5) no ExpandStage — do contrário a pausa HITL seria 'clarify_query'
    # em vez da 'source_plan' que este teste valida.
    query = "melhores ferramentas de observabilidade para microserviços"

    # Start research in background
    task = asyncio.create_task(orc.research(query, session_id=session_id))

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
