"""
Confluence Client — Conector para Confluence API (Enterprise RAG).

Este módulo implementa a busca em páginas e spaces do Confluence,
integrando-se ao pipeline SRA para incluir documentação corporativa
e wikis interno como fontes de pesquisa.

Requisitos:
- Atlassian API Token (variável CONFLUENCE_API_TOKEN ou CONFLUENCE_USERNAME/CONFLUENCE_API_TOKEN)
- URL base do Confluence (ex: https://yourcompany.atlassian.net/wiki)

Uso:
    from src.connectors.confluence_client import ConfluenceClient

    client = ConfluenceClient(
        base_url=os.getenv("CONFLUENCE_BASE_URL"),
        username=os.getenv("CONFLUENCE_USERNAME"),
        api_token=os.getenv("CONFLUENCE_API_TOKEN"),
    )
    results = await client.search("microservices patterns")
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.connectors.base_connector import BaseConnectorImplementation
from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("connectors.confluence")


class ConfluenceClient(BaseConnectorImplementation):
    """Cliente de busca para Confluence REST API.

    Suporta:
    - Busca em páginas (cql - confluence query language)
    - Busca em attachments
    - Filtros por space, tipo, data
    - Paginação automática
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: int = 30,
        cache: Optional[Any] = None,
        max_results: int = 25,
        enabled: bool = True,
    ) -> None:
        """Inicializa o cliente Confluence.

        Args:
            base_url: URL base do Confluence (ex: https://company.atlassian.net/wiki).
            username: Email/username do Atlassian.
            api_token: API token (não a senha).
            timeout: Timeout de requisição em segundos.
            cache: Instância de Cache para deduplicação.
            max_results: Máximo de resultados por busca.
            enabled: Se False, ignora chamadas de busca.
        """
        # Atlassian usa Basic Auth com username:api_token
        self.api_key = f"{username}:{api_token}" if username and api_token else None
        super().__init__(
            api_key=self.api_key, base_url=base_url, timeout=timeout, cache=cache
        )
        self.max_results = max_results
        self.enabled = enabled and bool(self.api_key)

        self._http_client = None

        if not self.enabled:
            logger.warning(
                "ConfluenceClient: desativado (credenciais ausentes ou enabled=False). "
                "Configure CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME e CONFLUENCE_API_TOKEN."
            )

        # Extrai a URL base limpa (remove /wiki se presente)
        if self.base_url and "/wiki" in self.base_url:
            self.api_base = self.base_url.rstrip("/")
        else:
            self.api_base = f"{self.base_url}/wiki" if self.base_url else None

    def _get_headers(self) -> Dict[str, str]:
        """Headers HTTP padrão para requisições Confluence."""
        import base64

        if self.api_key:
            encoded = base64.b64encode(self.api_key.encode()).decode()
            return {
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        return {"Content-Type": "application/json", "Accept": "application/json"}

    async def _get_http_client(self) -> Any:
        """Obtém ou cria o cliente HTTP (lazy-loaded)."""
        if self._http_client is None:
            try:
                import httpx

                self._http_client = httpx.AsyncClient(
                    base_url=self.api_base or "",
                    timeout=self.timeout,
                )
            except ImportError:
                logger.error("httpx não instalado. Instale com 'pip install httpx'.")
                raise
        return self._http_client

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca no Confluence via CQL.

        Args:
            query: Query de busca.
            **kwargs: Parâmetros adicionais:
                - space: Filtrar por Space Key
                - content_type: Tipo de conteúdo (page, blogpost, etc.)
                - limit: Sobrescrever max_results

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            logger.debug("ConfluenceClient: busca ignorada (desativado).")
            return []

        # Cache check
        cache_key = f"confluence:search:{query}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            client = await self._get_http_client()

            # CQL (Confluence Query Language) para busca avançada
            cql = self._build_cql(query, **kwargs)

            params = {
                "cql": cql,
                "limit": kwargs.get("limit", self.max_results),
                "expand": "content,metadata,history",
            }

            response = await client.get(
                "/rest/api/content/search", headers=self._get_headers(), params=params
            )
            response.raise_for_status()
            data = response.json()

            results = self._parse_results(data)

            await self._cache_set(cache_key, results)

            logger.info(
                "ConfluenceClient: %d resultados para '%s'", len(results), query
            )
            return results

        except Exception as e:
            logger.warning("ConfluenceClient: falha na busca '%s': %s", query, str(e))
            return []

    def _build_cql(self, query: str, **kwargs) -> str:
        """Constrói query CQL a partir de parâmetros.

        Args:
            query: Termos de busca.
            **kwargs: Filtros adicionais.

        Returns:
            str: Query CQL pronta.
        """
        cql_parts = [f'text ~ "{query}"']

        if space := kwargs.get("space"):
            cql_parts.append(f'space = "{space}"')

        if content_type := kwargs.get("content_type"):
            cql_parts.append(f'type = "{content_type}"')

        return " AND ".join(cql_parts)

    def _parse_results(self, data: Dict[str, Any]) -> List[SearchResult]:
        """Parseia resultados da API Confluence para SearchResult.

        Args:
            data: Resposta JSON da API.

        Returns:
            List[SearchResult]: Resultados normalizados.
        """
        results: List[SearchResult] = []
        for content in data.get("results", []):
            try:
                result = SearchResult(
                    source="confluence",
                    title=content.get("title", "Untitled"),
                    url=content.get("_links", {}).get("webui", ""),
                    description=content.get("excerpt", "") or content.get("title", ""),
                    metrics={
                        "last_modified": content.get("history", {})
                        .get("lastUpdated", {})
                        .get("createdAt"),
                        "content_id": content.get("id"),
                        "content_type": content.get("type"),
                        "space_key": content.get("space", {}).get("key"),
                    },
                    raw=content,
                )
                results.append(result)
            except Exception as e:
                logger.debug("ConfluenceClient: falha ao parsear resultado: %s", e)
                continue
        return results

    async def search_attachments(
        self, page_id: str, query: str, **kwargs
    ) -> List[SearchResult]:
        """Busca em attachments de uma página específica.

        Args:
            page_id: ID da página Confluence.
            query: Query de busca nos nomes dos arquivos.
            **kwargs: Parâmetros adicionais.

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            return []

        try:
            client = await self._get_http_client()
            params = {"limit": self.max_results, "filename": query}
            response = await client.get(
                f"/rest/api/content/{page_id}/child/attachment",
                headers=self._get_headers(),
                params=params,
            )
            response.raise_for_status()

            results: List[SearchResult] = []
            for attachment in response.json().get("results", []):
                result = SearchResult(
                    source="confluence",
                    title=attachment.get("title", "Attachment"),
                    url=attachment.get("_links", {}).get("webui", ""),
                    description=attachment.get("comment", ""),
                    metrics={
                        "file_size": attachment.get("size"),
                        "content_type": attachment.get("contentType"),
                    },
                    raw=attachment,
                )
                results.append(result)
            return results

        except Exception as e:
            logger.warning("ConfluenceClient: falha ao buscar attachments: %s", e)
            return []

    async def close(self) -> None:
        """Libera recursos HTTP."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        await super().close()


# ── Mock Confluence Client para testes offline ──────────────────────────────
class MockConfluenceClient:
    """Mock leve para testes e ambientes sem credenciais Confluence."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._closed = False

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        time.sleep(0.01)

        return [
            SearchResult(
                source="confluence",
                title=f"Mock Confluence Page: {query}",
                url="https://mock.atlassian.net/wiki/spaces/TEST/pages/123",
                description=f"Mock page documentation for: {query}",
                metrics={"mock": True, "space": "TEST"},
                raw={"mock": True, "query": query},
            ),
        ]

    async def close(self) -> None:
        self._closed = True

    __aenter__ = __aexit__ = lambda self: None  # type: ignore
