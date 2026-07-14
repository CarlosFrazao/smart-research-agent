"""
Notion Client — Conector para Notion API (Enterprise RAG).

Este módulo implementa a busca em documentos Notion via API oficial,
integrando-se ao pipeline SRA para incluir documentação corporativa
e wikis privados como fontes de pesquisa.

Requisitos:
- Notion API Key (variável de ambiente NOTION_API_KEY)
- Integration Token com permissões de leitura
- (Opcional) Database IDs para busca direcionada

Uso:
    from src.connectors.notion_client import NotionClient

    client = NotionClient(api_key=os.getenv("NOTION_API_KEY"))
    results = await client.search("project architecture")

    # Resultados são SearchResult compatíveis com o pipeline
    for r in results:
        print(r.title, r.url)
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.connectors.base_connector import BaseConnectorImplementation
from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("connectors.notion")


class NotionClient(BaseConnectorImplementation):
    """Cliente de busca para Notion API v1.

    Suporta:
    - Busca em pages e databases
    - Filtragem por tipo de conteúdo
    - Paginação automática
    - Cache de resultados
    """

    NOTION_API_BASE = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 30,
        cache: Optional[Any] = None,
        max_results: int = 25,
        enabled: bool = True,
    ) -> None:
        """Inicializa o cliente Notion.

        Args:
            api_key: Chave de API Notion (NOTION_API_KEY).
            base_url: URL customizada da API (para testes).
            timeout: Timeout de requisição em segundos.
            cache: Instância de Cache para deduplicação.
            max_results: Máximo de resultados por busca.
            enabled: Se False, o cliente ignora chamadas de busca.
        """
        super().__init__(
            api_key=api_key, base_url=base_url, timeout=timeout, cache=cache
        )
        self.max_results = max_results
        self.enabled = enabled and bool(api_key)

        # HTTP client lazy-loaded
        self._http_client = None

        if not self.enabled:
            logger.warning(
                "NotionClient: desativado (API key ausente ou enabled=False). "
                "Configure NOTION_API_KEY para habilitar."
            )

    def _get_headers(self) -> Dict[str, str]:
        """Headers HTTP padrão para requisições Notion."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _get_http_client(self) -> Any:
        """Obtém ou cria o cliente HTTP (lazy-loaded)."""
        if self._http_client is None:
            try:
                import httpx

                self._http_client = httpx.AsyncClient(
                    base_url=self.base_url or self.NOTION_API_BASE,
                    timeout=self.timeout,
                )
            except ImportError:
                logger.error("httpx não instalado. Instale com 'pip install httpx'.")
                raise
        return self._http_client

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca no Notion.

        Args:
            query: Query de busca.
            **kwargs: Parâmetros adicionais:
                - filter: Filtro de tipo (page, database, etc.)
                - sort: Ordem de resultados
                - start_cursor: Paginação

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            logger.debug("NotionClient: busca ignorada (desativado).")
            return []

        # Verificar cache primeiro
        cache_key = f"notion:search:{query}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            logger.debug("NotionClient: resultado encontrado no cache.")
            return cached

        try:
            client = await self._get_http_client()
            url = "/search"

            payload = {
                "query": query,
                "limit": self.max_results,
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            }

            # Filtros adicionais via kwargs
            if kwargs.get("filter"):
                payload["filter"] = kwargs["filter"]

            response = await client.post(url, headers=self._get_headers(), json=payload)
            response.raise_for_status()
            data = response.json()

            results = self._parse_results(data, query)

            # Cache resultados
            await self._cache_set(cache_key, results)

            logger.info("NotionClient: %d resultados para '%s'", len(results), query)
            return results

        except Exception as e:
            logger.warning("NotionClient: falha na busca '%s': %s", query, str(e))
            return []

    def _parse_results(self, data: Dict[str, Any], query: str) -> List[SearchResult]:
        """Parseia resultados da API Notion para SearchResult.

        Args:
            data: Resposta JSON da API Notion.
            query: Query original (para logging).

        Returns:
            List[SearchResult]: Resultados normalizados.
        """
        results: List[SearchResult] = []
        for obj in data.get("results", []):
            try:
                result = self._extract_page_info(obj)
                if result:
                    results.append(result)
            except Exception as e:
                logger.debug("NotionClient: falha ao parsear resultado: %s", e)
                continue
        return results

    def _extract_page_info(self, page: Dict[str, Any]) -> Optional[SearchResult]:
        """Extrai informações relevantes de um objeto Notion page.

        Args:
            page: Objeto page da API Notion.

        Returns:
            SearchResult ou None se não for extraível.
        """
        if page.get("object") != "page":
            return None

        properties = page.get("properties", {})
        title_prop = properties.get("title", {})

        # Extrair título
        title = ""
        if isinstance(title_prop, dict):
            title_parts = title_prop.get("title", [])
            title = (
                "".join(t.get("plain_text", "") for t in title_parts) or "Notion Page"
            )

        # Extrair URL
        url = page.get("url", "")
        if not url and page.get("id"):
            # Gerar URL placeholder se não existir
            url = f"notion://page/{page.get('id')}"

        # Métricas Notion específicas
        metrics = {
            "last_edited_time": page.get("last_edited_time"),
            "created_time": page.get("created_time"),
            "has_children": page.get("has_children", False),
            "parent_type": page.get("parent", {}).get("type"),
        }

        return SearchResult(
            source="notion",
            title=title,
            url=url,
            description=page.get("description", ""),
            metrics=metrics,
            raw=page,
        )

    async def search_databases(
        self, database_id: str, query: str, filter_property: Optional[str] = None
    ) -> List[SearchResult]:
        """Busca em uma database específica do Notion.

        Args:
            database_id: ID da database Notion.
            query: Query de busca nos conteúdos.
            filter_property: Propriedade para filtrar (ex: "Status", "Category").

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            return []

        try:
            client = await self._get_http_client()
            url = f"/databases/{database_id}/query"

            payload = {
                "query": query,
                "page_size": self.max_results,
            }

            if filter_property:
                payload["filter"] = {
                    "property": filter_property,
                    "rich_text": {"equals": query},
                }

            response = await client.post(url, headers=self._get_headers(), json=payload)
            response.raise_for_status()
            data = response.json()

            results: List[SearchResult] = []
            for page in data.get("results", []):
                if page.get("object") == "page":
                    result = self._extract_page_info(page)
                    if result:
                        results.append(result)

            return results

        except Exception as e:
            logger.warning(
                "NotionClient: falha na busca database %s: %s", database_id, str(e)
            )
            return []

    async def close(self) -> None:
        """Libera recursos HTTP."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        await super().close()


# ── Mock Notion Client para testes offline ───────────────────────────────────
class MockNotionClient:
    """Mock leve para testes e ambientes sem credenciais Notion.

    Gera resultados simulados compatíveis com a interface real, permitindo
    testes de integração sem depender de API externa.
    """

    def __init__(self, enabled: bool = True) -> None:
        """Inicializa o mock.

        Args:
            enabled: Se False, retorna lista vazia (simula desativação).
        """
        self.enabled = enabled
        self._closed = False

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Retorna resultados simulados."""
        if not self.enabled:
            return []

        time.sleep(0.01)  # Pequeno delay para realismo

        return [
            SearchResult(
                source="notion",
                title=f"Mock Result 1: {query}",
                url="https://mock.notion.so/page1",
                description=f"Mock description for query: {query}",
                metrics={"mock": True, "confidence_score": 0.9},
                raw={"mock": True, "query": query},
            ),
            SearchResult(
                source="notion",
                title=f"Mock Result 2: {query} (Draft)",
                url="https://mock.notion.so/page2",
                description=f"Draft document containing: {query}",
                metrics={"mock": True, "confidence_score": 0.7},
                raw={"mock": True, "query": query, "status": "draft"},
            ),
        ]

    async def close(self) -> None:
        """Libera recursos."""
        self._closed = True

    # Protocol compatibility
    __aenter__ = __aexit__ = lambda self: None  # type: ignore
