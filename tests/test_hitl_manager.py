import asyncio
import pytest
from src.hitl_manager import HITLManager
from fastapi.testclient import TestClient
from src.mcp_server import create_app


@pytest.mark.asyncio
async def test_hitl_approval_success():
    manager = HITLManager()
    session_id = "session_1"
    request_data = {"outline": "Introdução, Conclusão"}
    approved_data = {"outline": "Introdução, Desenvolvimento, Conclusão"}

    # Inicia a tarefa de aprovação em background
    task = asyncio.create_task(
        manager.request_approval(session_id, "outline", request_data, timeout=5.0)
    )

    # Aguarda um pequeno delay para garantir que a suspensão ocorreu
    await asyncio.sleep(0.1)

    # Verifica que a requisição está pendente
    pending = manager.get_pending_request(session_id)
    assert pending is not None
    assert pending["request_type"] == "outline"
    assert pending["data"] == request_data

    # Submete a resposta
    success = await manager.submit_response(session_id, approved_data)
    assert success is True

    # Aguarda o retorno da tarefa suspensa
    result = await task
    assert result == approved_data


@pytest.mark.asyncio
async def test_hitl_approval_timeout():
    manager = HITLManager()
    session_id = "session_timeout"
    request_data = {"outline": "Dados Originais"}

    # Executa a aprovação com timeout de 0.2 segundos e sem submeter resposta
    result = await manager.request_approval(
        session_id, "outline", request_data, timeout=0.2
    )

    # Deve retornar os dados originais em caso de timeout
    assert result == request_data

    # Deve limpar a requisição pendente
    assert manager.get_pending_request(session_id) is None


def test_hitl_endpoints():
    app = create_app()
    client = TestClient(app)
    container = app.state.container
    hitl: HITLManager = container.resolve("hitl_manager")

    # Garante que começa vazio
    response = client.get("/api/v1/hitl/pending")
    assert response.status_code == 200
    assert response.json() == {"pending_requests": {}}

    # Tenta dar resume em id inválido
    response = client.post("/api/v1/hitl/resume/invalid_id", json={"approved_data": {}})
    assert response.status_code == 404

    # Moca uma requisição ativa direto no manager
    async def mock_pending():
        await hitl._lock.acquire()
        try:
            hitl._events["session_api_test"] = asyncio.Event()
            hitl._requests["session_api_test"] = {
                "request_type": "outline",
                "data": {"outline": "Original"},
                "created_at": "timestamp",
                "timeout": 10.0,
            }
        finally:
            hitl._lock.release()

    asyncio.run(mock_pending())

    # Verifica se aparece na listagem geral
    response = client.get("/api/v1/hitl/pending")
    assert response.status_code == 200
    assert "session_api_test" in response.json()["pending_requests"]

    # Verifica se recupera metadados da sessão individual
    response = client.get("/api/v1/hitl/pending/session_api_test")
    assert response.status_code == 200
    assert response.json()["request_type"] == "outline"

    # Libera a sessão via endpoint REST
    response = client.post(
        "/api/v1/hitl/resume/session_api_test",
        json={"approved_data": {"outline": "Aprovado via API"}},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "Sessão 'session_api_test' retomada.",
    }
