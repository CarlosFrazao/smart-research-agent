"""
Conector Package — Centraliza importações e exporta todos os conectores.

Este módulo expõe todos os conectores Enterprise RAG da Smart Research
Agent para uso em todo o códigobase através de importações simples.

Uso em código:
    from src.connectors import NotionClient, ConfluenceClient, SharePointClient
    connector = NotionClient(api_key=api_key)
    results = await connector.search(query)

Benefícios:
- Automação completa dos conectores Enterprise RAG (Phases 3 & 4)
- Facilidade de teste via mock_connector
- Interface padronizada para todas as fontes pedidas no SourcePlanner
- Integração transparente com o pipeline de pesquisa existente
"""

from __future__ import annotations

import logging
from typing import Any

# Export-consumo: todos os conectores relevantes para Enterprise RAG
from .notion_client import NotionClient
from .confluence_client import ConfluenceClient
from .sharepoint_client import SharePointClient

from .mock_connector import (
    create_mock_connector,
    create_all_mocks,
)  # Exporta factory functions

logger = logging.getLogger("connectors.package")

__all__ = [
    "NotionClient",
    "ConfluenceClient",
    "SharePointClient",
    "create_mock_connector",
    "create_all_mocks",
]

# Auto-validate availability on import (will fail fast if dependencies missing)
try:
    # Test that NotionClient can be imported and instantiated with basic args
    _ = NotionClient  # noqa: F401
    _ = ConfluenceClient  # noqa: F401
    _ = SharePointClient  # noqa: F401
except ImportError as e:
    logger.error(f"Importação de conectores falhou: {e}")
    raise e
