"""
Testes para conectores Enterprise RAG (Notion, Confluence, SharePoint).

Testa:
- Funcionamento dos mocks em ambientes offline
- Interface comum entre todos os conectores
- Tratamento de erros e fallbacks
- Cache de resultados
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.connectors.mock_connector import (
    MockNotionClient,
    MockConfluenceClient,
    MockSharePointClient,
    create_mock_connector,
    create_all_mocks,
)
from src.connectors.base_connector import BaseConnectorImplementation
from src.types import SearchResult


class TestMockNotionClient:
    """Testes para MockNotionClient."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """MockNotionClient.search deve retornar resultados simulados."""
        client = MockNotionClient(enabled=True, delay=0)
        results = await client.search("test query")

        assert len(results) >= 1
        assert all(isinstance(r, SearchResult) for r in results)
        assert all(r.source == "notion" for r in results)

    @pytest.mark.asyncio
    async def test_search_disabled_returns_empty(self):
        """Quando desativado, deve retornar lista vazia."""
        client = MockNotionClient(enabled=False)
        results = await client.search("test query")

        assert results == []

    @pytest.mark.asyncio
    async def test_close_sets_flag(self):
        """close() deve marcar o cliente como fechado."""
        client = MockNotionClient()
        await client.close()

        assert client._closed is True


class TestMockConfluenceClient:
    """Testes para MockConfluenceClient."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """MockConfluenceClient.search deve retornar resultados simulados."""
        client = MockConfluenceClient(enabled=True, delay=0)
        results = await client.search("architecture")

        assert len(results) >= 1
        assert all(r.source == "confluence" for r in results)

    @pytest.mark.asyncio
    async def test_result_has_space_metadata(self):
        """Resultados devem ter metadata de space."""
        client = MockConfluenceClient()
        results = await client.search("test")

        for r in results:
            # Check that either raw contains space_key or metrics contains space
            assert "space_key" in r.raw or "space" in r.metrics


class TestMockSharePointClient:
    """Testes para MockSharePointClient."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """MockSharePointClient.search deve retornar resultados simulados."""
        client = MockSharePointClient(enabled=True, delay=0)
        results = await client.search("report")

        assert len(results) >= 1
        assert all(r.source == "sharepoint" for r in results)

    @pytest.mark.asyncio
    async def test_result_has_file_metadata(self):
        """Resultados devem ter metadata de arquivo."""
        client = MockSharePointClient()
        results = await client.search("test")

        for r in results:
            assert "file_type" in r.metrics or "site" in r.raw.get("mock", {})


class TestMockFactory:
    """Testes para factory functions de mock."""

    def test_create_mock_connector_notion(self):
        """Factory deve criar MockNotionClient corretamente."""
        client = create_mock_connector("notion", enabled=False)
        assert isinstance(client, MockNotionClient)
        assert client.enabled is False

    def test_create_mock_connector_confluence(self):
        """Factory deve criar MockConfluenceClient corretamente."""
        client = create_mock_connector("confluence")
        assert isinstance(client, MockConfluenceClient)
        assert client.enabled is True

    def test_create_mock_connector_sharepoint(self):
        """Factory deve criar MockSharePointClient corretamente."""
        client = create_mock_connector("sharepoint")
        assert isinstance(client, MockSharePointClient)

    def test_create_mock_connector_unknown_raises(self):
        """Factory deve levantar ValueError para fonte desconhecida."""
        with pytest.raises(ValueError, match="Fonte desconhecida"):
            create_mock_connector("unknown")

    @pytest.mark.asyncio
    async def test_create_all_mocks(self):
        """create_all_mocks deve retornar todos os conectores."""
        mocks = create_all_mocks(enabled=True)

        assert "notion" in mocks
        assert "confluence" in mocks
        assert "sharepoint" in mocks
        assert all(m.enabled for m in mocks.values())


class TestBaseConnectorImplementation:
    """Testes para BaseConnectorImplementation."""

    def test_normalize_result(self):
        """_normalize_result deve converter dicionário bruto para SearchResult."""
        connector = BaseConnectorImplementation()

        raw = {
            "title": "Test Title",
            "url": "https://example.com",
            "description": "Test description",
            "custom_field": "value",
        }

        result = connector._normalize_result(raw, "test_source")

        assert isinstance(result, SearchResult)
        assert result.source == "test_source"
        assert result.title == "Test Title"
        assert result.url == "https://example.com"
        assert result.description == "Test description"
        assert result.raw == raw


class TestIntegrationWithPipeline:
    """Testes de integração com o pipeline."""

    @pytest.mark.asyncio
    async def test_mock_connector_compatible_with_pipeline(self):
        """Mock connectors devem ser compatíveis com a assinatura do pipeline."""
        client = MockNotionClient()

        # Simular o que o pipeline faria
        results = await client.search("query")
        await client.close()

        # Verificações de compatibilidade
        assert isinstance(results, list)
        assert all(isinstance(r, SearchResult) for r in results)

    @pytest.mark.asyncio
    async def test_cache_integration(self):
        """Cache deve funcionar com conectores mock."""
        import tempfile
        import os

        # Create a temp directory for cache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "cache")
            os.makedirs(cache_dir, exist_ok=True)

            from src.cache import Cache
            cache = Cache(cache_dir=cache_dir)

            client = MockNotionClient(cache=cache)
            results1 = await client.search("cached query")
            results2 = await client.search("cached query")

            # Segunda chamada deve retornar do cache
            assert len(results2) == len(results1)
            assert results2[0].title == results1[0].title
            assert results2[0].url == results1[0].url
            assert results2[0].description == results1[0].description
