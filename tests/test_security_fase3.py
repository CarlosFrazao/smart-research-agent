"""
Testes de segurança da API REST (Auditoria Parte 2 — Fase 3):
- 3.1: Autenticação via X-API-Key (Depends verify_api_key)
- 3.2: CORS restrito por CORS_ALLOWED_ORIGINS
- 3.3: Rate limiting por IP (slowapi)

Estratégia: forçamos a Config efetiva do singleton `config_manager`
ANTES de (re)importar `api.main`, de modo que o middleware de CORS (construído
uma única vez na importação) reflita a origem desejada, e o `get_config`
usado por `verify_api_key` devolva a mesma Config.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from src.config import config_manager


def _client_with(override: dict) -> TestClient:
    """Cria um TestClient com app recém-construído sob a Config forçada."""
    forced = config_manager.config.model_copy(update=override)
    config_manager._config = forced  # fonte da verdade para CORS + get_config

    import api.main as api_main

    importlib.reload(api_main)
    return TestClient(api_main.app, raise_server_exceptions=False)


# ─── 3.1 — Autenticação via X-API-Key ─────────────────────────────────


def test_research_requires_api_key_when_configured():
    """Com SRA_API_KEY configurada, /api/research sem header retorna 401."""
    client = _client_with({"sra_api_key": "segredo-123"})
    response = client.post(
        "/api/research",
        json={"query": "teste de auth", "mode": "cirurgia"},
    )
    assert response.status_code == 401
    assert "API Key" in response.json()["detail"]


def test_research_accepts_valid_api_key():
    """Com SRA_API_KEY configurada, header correto passa (não é 401)."""
    import src.orchestrator as orch_mod
    from unittest.mock import AsyncMock, patch

    client = _client_with({"sra_api_key": "segredo-123"})
    with patch.object(orch_mod, "Orchestrator") as mock_orc:
        mock_orc.return_value.research = AsyncMock(return_value="# Mocked Report")
        response = client.post(
            "/api/research",
            json={"query": "teste de auth", "mode": "cirurgia"},
            headers={"X-API-Key": "segredo-123"},
        )
    # Autenticação validou: não é 401. Com Orchestrator mockado, retorna 201.
    assert response.status_code != 401


def test_research_rejects_wrong_api_key():
    """Com SRA_API_KEY configurada, header errado retorna 401."""
    client = _client_with({"sra_api_key": "segredo-123"})
    response = client.post(
        "/api/research",
        json={"query": "teste", "mode": "cirurgia"},
        headers={"X-API-Key": "errada"},
    )
    assert response.status_code == 401


def test_research_open_without_api_key_configured():
    """Sem SRA_API_KEY, /api/research fica aberto (backward compatibility)."""
    import src.orchestrator as orch_mod
    from unittest.mock import AsyncMock, patch

    client = _client_with({"sra_api_key": None})
    with patch.object(orch_mod, "Orchestrator") as mock_orc:
        mock_orc.return_value.research = AsyncMock(return_value="# Mocked Report")
        response = client.post(
            "/api/research",
            json={"query": "teste", "mode": "cirurgia"},
        )
    assert response.status_code != 401


def test_health_open_without_api_key():
    """/health nunca deve exigir autenticação."""
    client = _client_with({"sra_api_key": "segredo-123"})
    response = client.get("/health")
    assert response.status_code == 200


# ─── 3.2 — CORS restrito ──────────────────────────────────────────────


def test_cors_allows_configured_origin():
    """Uma origem presente em CORS_ALLOWED_ORIGINS recebe header CORS."""
    client = _client_with({"cors_allowed_origins": ["https://app.exemplo.com"]})
    response = client.get(
        "/health",
        headers={"Origin": "https://app.exemplo.com"},
    )
    assert response.status_code == 200
    assert (
        response.headers.get("access-control-allow-origin")
        == "https://app.exemplo.com"
    )


def test_cors_reflects_wildcard_by_default():
    """Default (["*"]) permite qualquer origem (CORS reflete a origem pedida)."""
    client = _client_with({"cors_allowed_origins": ["*"]})
    response = client.get(
        "/health",
        headers={"Origin": "https://qualquer.com"},
    )
    assert response.status_code == 200
    # Com allow_origins=["*"], o Starlette reflete a origem solicitada e
    # sinaliza "vary: origin" — qualquer origem é aceita.
    assert response.headers.get("access-control-allow-origin") == "https://qualquer.com"
    assert "origin" in response.headers.get("vary", "").lower()


# ─── 3.3 — Rate limiting por IP (slowapi) ───────────────────────────


def test_rate_limit_enforced_on_research():
    """Envia 11 requisições ao endpoint de pesquisa; a 11ª deve ser 429.

    O limite configurado é 10/minute. Como o rate limiter do slowapi roda
    antes do corpo do endpoint, o bloqueio ocorre independentemente de o
    pipeline de pesquisa falhar por falta de LLM.
    """
    import src.orchestrator as orch_mod
    from unittest.mock import AsyncMock, patch

    # Configuração sem auth para focar só no rate limiting.
    client = _client_with({"sra_api_key": None})
    with patch.object(orch_mod, "Orchestrator") as mock_orc:
        instance = mock_orc.return_value
        instance.research = AsyncMock(return_value="# Mocked Report")

        status_codes: list[int] = []
        for _ in range(11):
            resp = client.post(
                "/api/research",
                json={"query": "teste", "mode": "cirurgia"},
            )
            status_codes.append(resp.status_code)

    assert 429 in status_codes  # limite estourou
    assert 401 not in status_codes  # auth desativada


def test_health_not_rate_limited():
    """/health não tem rate limit e responde 200 repetidamente."""
    client = _client_with({"sra_api_key": None})
    codes = [client.get("/health").status_code for _ in range(12)]
    assert all(c == 200 for c in codes)
