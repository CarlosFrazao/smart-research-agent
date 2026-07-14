"""
Mock Connectors — Implementações de mock para testes offline dos conectores Enterprise RAG.

Este módulo fornece mocks leves e realistas para Notion, Confluence e SharePoint,
permitindo testes de integração sem depender de APIs externas ou credenciais.

Todos os mocks implementam a mesma interface dos clientes reais:
- async search(query: str, **kwargs) -> List[SearchResult]
- async close() -> None
"""

from __future__ import annotations

from typing import Dict, List

from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("connectors.mock")


class BaseMockConnector:
    """Classe base para mocks de conectores."""

    def __init__(
        self,
        source_name: str,
        enabled: bool = True,
        delay: float = 0.01,
        results_per_query: int = 2,
    ) -> None:
        """Inicializa o mock base.

        Args:
            source_name: Nome da fonte (ex: "notion", "confluence", "sharepoint").
            enabled: Se False, retorna lista vazia.
            delay: Delay artificial em segundos para simular latência de rede.
            results_per_query: Número de resultados simulados por query.
        """
        self.source_name = source_name
        self.enabled = enabled
        self.delay = delay
        self.results_per_query = results_per_query
        self._closed = False

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Busca simulada."""
        if not self.enabled:
            return []

        if self.delay > 0:
            import asyncio

            await asyncio.sleep(self.delay)

        # Gerar resultados mock baseados na query
        results = []
        for i in range(1, self.results_per_query + 1):
            result = SearchResult(
                source=self.source_name,
                title=f"Mock {self.source_name.capitalize()} Result {i}: {query}",
                url=f"https://mock.{self.source_name}.com/result{i}",
                description=f"Mock description from {self.source_name} for query: {query}",
                metrics={
                    "mock": True,
                    "confidence_score": 0.8 + (i * 0.05),
                },
                raw={
                    "mock": True,
                    "query": query,
                    "result_index": i,
                },
            )
            results.append(result)

        return results

    async def close(self) -> None:
        """Fecha o mock."""
        self._closed = True


class MockNotionClient(BaseMockConnector):
    """Mock para Notion API."""

    def __init__(self, **kwargs):
        # Extract cache before calling super
        self.cache = kwargs.pop("cache", None)
        super().__init__("notion", **kwargs)

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        await super().search(query, **kwargs)

        # Resultados Notion-específicos
        import asyncio

        if self.delay > 0:
            await asyncio.sleep(self.delay)

        return [
            SearchResult(
                source="notion",
                title=f"Notion Page: {query}",
                url=f"https://mock.notion.so/page-{query.lower().replace(' ', '-')}",
                description=f"Comprehensive documentation about {query} with examples and best practices.",
                metrics={
                    "mock": True,
                    "last_edited": "2024-01-15T10:30:00Z",
                    "has_children": True,
                },
                raw={"mock": True, "type": "page", "query": query},
            ),
            SearchResult(
                source="notion",
                title=f"Notion Database: {query} Projects",
                url=f"https://mock.notion.so/db-{query.lower().replace(' ', '-')}",
                description=f"Project tracking database for {query} with status and assignees.",
                metrics={
                    "mock": True,
                    "row_count": 42,
                    "database": True,
                },
                raw={"mock": True, "type": "database", "query": query},
            ),
        ]


class MockConfluenceClient(BaseMockConnector):
    """Mock para Confluence API."""

    def __init__(self, **kwargs):
        super().__init__("confluence", **kwargs)

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        import asyncio

        if self.delay > 0:
            await asyncio.sleep(self.delay)

        return [
            SearchResult(
                source="confluence",
                title=f"Confluence Page: {query} Documentation",
                url="https://mock.atlassian.net/wiki/spaces/TECH/pages/123456",
                description=f"Technical documentation and guidelines for {query} implementation.",
                metrics={
                    "mock": True,
                    "space": "TECH",
                    "version": 5,
                },
                raw={"mock": True, "space_key": "TECH", "query": query},
            ),
            SearchResult(
                source="confluence",
                title=f"Blog Post: {query} Best Practices",
                url="https://mock.atlassian.net/wiki/spaces/TECH/blog/2024/01/15/789012",
                description=f"Latest best practices and lessons learned about {query}.",
                metrics={
                    "mock": True,
                    "space": "TECH",
                    "author": "dev-team",
                },
                raw={
                    "mock": True,
                    "space_key": "TECH",
                    "content_type": "blogpost",
                    "query": query,
                },
            ),
        ]


class MockSharePointClient(BaseMockConnector):
    """Mock para SharePoint API."""

    def __init__(self, **kwargs):
        super().__init__("sharepoint", **kwargs)

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        import asyncio

        if self.delay > 0:
            await asyncio.sleep(self.delay)

        return [
            SearchResult(
                source="sharepoint",
                title=f"SharePoint Document: {query} Report",
                url="https://company.sharepoint.com/sites/team/Shared%20Documents/report.docx",
                description=f"Quarterly report and analysis document for {query}.",
                metrics={
                    "mock": True,
                    "file_type": "docx",
                    "size_mb": 2.4,
                },
                raw={"mock": True, "site": "team", "query": query},
            ),
            SearchResult(
                source="sharepoint",
                title=f"Presentation: {query} Architecture",
                url="https://company.sharepoint.com/sites/team/Shared%20Documents/arch.pptx",
                description=f"Architecture overview and decision records for {query}.",
                metrics={
                    "mock": True,
                    "file_type": "pptx",
                    "slide_count": 24,
                },
                raw={"mock": True, "site": "team", "query": query},
            ),
        ]


# ── Factory para criar mocks ────────────────────────────────────────────────
def create_mock_connector(source: str, **kwargs) -> BaseMockConnector:
    """Factory para criar mock do conector especificado.

    Args:
        source: Nome da fonte ("notion", "confluence", "sharepoint").
        **kwargs: Parâmetros do mock (enabled, delay, results_per_query).

    Returns:
        BaseMockConnector: Instância do mock apropriado.

    Raises:
        ValueError: Se a fonte não for reconhecida.
    """
    mocks = {
        "notion": MockNotionClient,
        "confluence": MockConfluenceClient,
        "sharepoint": MockSharePointClient,
    }

    if source not in mocks:
        raise ValueError(
            f"Fonte desconhecida: {source}. Disponíveis: {list(mocks.keys())}"
        )

    return mocks[source](**kwargs)


def create_all_mocks(enabled: bool = True, **kwargs) -> Dict[str, BaseMockConnector]:
    """Cria todos os mocks de conectores Enterprise RAG.

    Args:
        enabled: Se todos os mocks devem ser habilitados.
        **kwargs: Parâmetros adicionais passados para cada mock.

    Returns:
        Dict[str, BaseMockConnector]: Dicionário com todos os mocks.
    """
    return {
        "notion": MockNotionClient(enabled=enabled, **kwargs),
        "confluence": MockConfluenceClient(enabled=enabled, **kwargs),
        "sharepoint": MockSharePointClient(enabled=enabled, **kwargs),
    }
