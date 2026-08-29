"""Shared REST security utilities for the Smart Research Agent.

Centralizes CORS middleware setup, rate limiter configuration, and API key
verification so both ``api/main.py`` (legacy app) and ``src/mcp_server.py``
(official server) use the same security primitives without duplication.

.. note:: **P1-1 refactor:** This module was extracted from duplicated code
   that previously lived in both ``api/main.py`` and ``src/mcp_server.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

if TYPE_CHECKING:  # evita import circular em runtime
    from src.config import Config

from src.config import config_manager, get_config

logger = logging.getLogger(__name__)

# ── API Key header ────────────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
    config: "Config" = Depends(get_config),
) -> None:
    """Verifica a API Key do SRA nos endpoints de pesquisa.

    Se ``SRA_API_KEY`` não estiver configurada no ``.env``, a autenticação é
    desabilitada (compatibilidade com uso local sem configuração) e a função
    retorna sem erro. Caso contrário, exige que o header ``X-API-Key`` traga
    exatamente o valor configurado.

    Args:
        api_key: Valor do header ``X-API-Key`` (ou ``None`` se ausente).
        config: Configuração efetiva do contexto (via ``get_config``).

    Raises:
        HTTPException: 401 se a chave estiver ausente ou incorreta.
    """
    if not config.sra_api_key:
        # Modo sem auth: o aviso já foi emitido no startup (uma vez).
        return
    if not api_key or api_key != config.sra_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. Use header: X-API-Key: <your-key>",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def get_rate_limit(config: "Config | None" = None) -> str:
    """Retorna o rate limit configurado (SRA_RATE_LIMIT, default '10/minute')."""
    cfg = config or config_manager.config
    return getattr(cfg, "rate_limit", "10/minute")


# Module-level singletons — populated by apply_rest_security() so both
# api/main.py and src/mcp_server.py can import them directly.
_RATE_LIMIT: str = "10/minute"
limiter: Limiter | None = None


def apply_rest_security(app: FastAPI, cfg: "Config | None" = None) -> None:
    """Aplica CORS, rate limiting e auth ao servidor FastAPI.

    Espelha as defesas implementadas em ``api/main.py`` e
    ``src/mcp_server.py`` (Auditoria Parte 2 — Fase 3):

    - **CORS:** origens lidas de ``cfg.cors_allowed_origins`` (env), não ``*``.
    - **Rate limiting:** por IP via slowapi (``app.state.limiter``); o limite é
      configurável via ``SRA_RATE_LIMIT``.
    - **Auth:** ``verify_api_key`` é usado como dependência nas rotas REST.

    Args:
        app: Instância FastAPI recém-criada.
        cfg: Configuração efetiva. Se omitido, usa ``config_manager.config``.
    """
    global _RATE_LIMIT, limiter

    if cfg is None:
        cfg = config_manager.config

    _RATE_LIMIT = get_rate_limit(cfg)

    try:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=getattr(cfg, "cors_allowed_origins", ["*"]),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[f"@{_RATE_LIMIT}"],
        )
        app.state.limiter = limiter
        app.state.rate_limit = _RATE_LIMIT
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

        if not getattr(cfg, "sra_api_key", None):
            logger.warning(
                "SRA_API_KEY não configurada. Rotas REST sem autenticação. "
                "Defina SRA_API_KEY no .env para uso em produção."
            )
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("api/security: não foi possível aplicar segurança REST: %s", exc)
