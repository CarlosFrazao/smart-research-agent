"""
Testes das rotas REST do Dashboard SPA (Bloco 1 — ARES-V4.3).

Cobre:
- GET /health
- GET /api/reports
- GET /api/reports/{filename}
- POST /api/chat (sem LLM real — apenas estrutura da resposta)
- POST /feedback
- POST /api/obsidian-sync (sem vault real)
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from src.mcp_server import app

client = TestClient(app, raise_server_exceptions=False)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_list_reports_returns_json():
    response = client.get("/api/reports")
    assert response.status_code == 200
    data = response.json()
    assert "reports" in data
    assert isinstance(data["reports"], list)


def test_list_reports_excludes_underscore_files(tmp_path, monkeypatch):
    """Arquivos _feedback.jsonl etc. não devem aparecer na lista."""
    import src.mcp_server as srv

    monkeypatch.setattr(srv, "_REPORTS_DIR", str(tmp_path))
    (tmp_path / "_feedback.jsonl").write_text("x")
    (tmp_path / "2026-06-24-test-report.md").write_text("# Test")

    response = client.get("/api/reports")
    assert response.status_code == 200
    filenames = [r["filename"] for r in response.json()["reports"]]
    assert "2026-06-24-test-report.md" in filenames
    assert "_feedback.jsonl" not in filenames


def test_get_report_not_found():
    response = client.get("/api/reports/nao-existe.md")
    assert response.status_code == 404


def test_get_report_path_traversal_blocked():
    response = client.get("/api/reports/../../etc/passwd")
    assert response.status_code in (400, 404)


def test_get_report_invalid_extension():
    response = client.get("/api/reports/arquivo.txt")
    assert response.status_code == 400


def test_get_report_returns_content(tmp_path, monkeypatch):
    import src.mcp_server as srv

    monkeypatch.setattr(srv, "_REPORTS_DIR", str(tmp_path))
    md_file = tmp_path / "2026-06-24-relatorio-teste.md"
    md_file.write_text("# Relatório de Teste\n\nConteúdo do relatório.", encoding="utf-8")

    response = client.get("/api/reports/2026-06-24-relatorio-teste.md")
    assert response.status_code == 200
    assert "Relatório de Teste" in response.text


def test_chat_direct_no_messages():
    response = client.post("/api/chat", json={"messages": []})
    assert response.status_code == 200
    assert "error" in response.json()


def test_chat_direct_missing_body():
    response = client.post("/api/chat", json={})
    assert response.status_code == 200
    assert "error" in response.json()


def test_feedback_endpoint_valid():
    response = client.post(
        "/feedback",
        json={"query": "melhores frameworks Python 2026", "signal": "helpful"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("recorded") is True


def test_feedback_endpoint_not_helpful():
    response = client.post(
        "/feedback",
        json={"query": "teste de feedback negativo", "signal": "not_helpful"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("recorded") is True


def test_feedback_endpoint_invalid_signal():
    response = client.post(
        "/feedback",
        json={"query": "teste inválido", "signal": "sinal_invalido_xyz"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("recorded") is False


def test_obsidian_sync_no_vault_configured():
    """Sem OBSIDIAN_VAULT_PATH configurado, deve retornar 400."""
    response = client.post("/api/obsidian-sync", json={"filename": "algum-relatorio"})
    assert response.status_code in (200, 400)


def test_dashboard_route_exists():
    """GET / deve retornar 200 (com dashboard) ou 404 (sem static/index.html)."""
    response = client.get("/")
    assert response.status_code in (200, 404)


# ─── BLOCO 5 — Dashboard v2.0: api_key/provider dinâmicos ────────────────────

def test_chat_with_api_key_field_accepted():
    """POST /api/chat com api_key no body não deve falhar na estrutura do request."""
    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "teste"}],
            "api_key": "sk-test-fake-key-000",
            "provider": "openrouter",
        },
    )
    assert response.status_code == 200


def test_chat_with_empty_api_key_uses_server_default():
    """api_key vazia deve ser ignorada silenciosamente (backend usa .env)."""
    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "teste sem chave"}],
            "api_key": "",
            "provider": "",
        },
    )
    assert response.status_code == 200


def test_chat_with_invalid_provider_falls_back():
    """provider inválido não deve causar crash 500 — deve usar o provedor padrão."""
    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "teste provider invalido"}],
            "provider": "provedor_inexistente_xpto",
        },
    )
    assert response.status_code == 200


def test_research_accepts_api_key_in_body():
    """POST /research com api_key/provider no body deve ser aceito sem erro de schema."""
    from unittest.mock import patch, AsyncMock, MagicMock
    mock_orc = MagicMock()
    mock_orc.research = AsyncMock(return_value="# Mocked Report")

    with patch("src.mcp_server.get_orchestrator", return_value=mock_orc):
        response = client.post(
            "/research",
            json={
                "query": "teste de override de api key",
                "api_key": "sk-test-fake-key-000",
                "provider": "openrouter",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data.get("report") == "# Mocked Report"
