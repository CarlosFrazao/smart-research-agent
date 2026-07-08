"""
SharePoint Client — Conector para Microsoft SharePoint (Enterprise RAG).

Este módulo implementa a busca em documentos SharePoint via Microsoft Graph API,
integrando-se ao pipeline SRA para incluir documentos corporativos
e arquivos de repositórios corporativos como fontes de pesquisa.

Requisitos:
- Azure AD App Registration com permissões Microsoft Graph
- Client ID, Client Secret ou Certificate
- Escopo de permissão: Sites.Read.All, Files.Read.All

Uso:
    from src.connectors.sharepoint_client import SharePointClient

    client = SharePointClient(
        tenant_id=os.getenv("AZURE_TENANT_ID"),
        client_id=os.getenv("AZURE_CLIENT_ID"),
        client_secret=os.getenv("AZURE_CLIENT_SECRET"),
        site_url=os.getenv("SHAREPOINT_SITE_URL"),
    )
    results = await client.search("quarterly report")
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.connectors.base_connector import BaseConnectorImplementation
from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("connectors.sharepoint")


class SharePointClient(BaseConnectorImplementation):
    """Cliente de busca para SharePoint via Microsoft Graph API.

    Suporta:
    - Busca em arquivos e páginas do SharePoint
    - Busca em OneDrive
    - Paginação automática
    - Cache de resultados
    """

    GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        certificate_thumbprint: Optional[str] = None,
        site_url: Optional[str] = None,
        timeout: int = 30,
        cache: Optional[Any] = None,
        max_results: int = 25,
        enabled: bool = True,
    ) -> None:
        """Inicializa o cliente SharePoint.

        Args:
            tenant_id: ID do tenant Azure AD.
            client_id: Client ID da app registration.
            client_secret: Client secret da app registration.
            certificate_thumbprint: Thumbprint do certificado (alternativa ao secret).
            site_url: URL do site SharePoint (ex: https://company.sharepoint.com/sites/team).
            timeout: Timeout de requisição em segundos.
            cache: Instância de Cache para deduplicação.
            max_results: Máximo de resultados por busca.
            enabled: Se False, ignora chamadas de busca.
        """
        # SharePoint usa Azure AD para autenticação
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.certificate_thumbprint = certificate_thumbprint
        self.site_url = site_url

        # API key é o token combinado
        api_key = f"{tenant_id}:{client_id}" if tenant_id and client_id else None
        super().__init__(api_key=api_key, base_url=self.GRAPH_API_BASE, timeout=timeout, cache=cache)

        self.max_results = max_results
        self.enabled = enabled and bool(tenant_id and (client_id or certificate_thumbprint))

        self._http_client = None
        self._access_token: Optional[str] = None

        if not self.enabled:
            logger.warning(
                "SharePointClient: desativado (credenciais ausentes ou enabled=False). "
                "Configure AZURE_TENANT_ID, AZURE_CLIENT_ID/CLIENT_SECRET e SHAREPOINT_SITE_URL."
            )

    async def _authenticate(self) -> str:
        """Obtém token de acesso via client credentials flow.

        Returns:
            str: Token de acesso válido.
        """
        if self._access_token:
            return self._access_token

        try:
            import httpx
            import aiohttp

            token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

            data = {
                "client_id": self.client_id,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }

            if self.client_secret:
                data["client_secret"] = self.client_secret
            elif self.certificate_thumbprint:
                # Autenticação com certificado requer lógica adicional
                raise NotImplementedError(
                    "Autenticação com certificado ainda não implementada. Use client_secret."
                )
            else:
                raise ValueError("Nenhuma credencial fornecida (client_secret ou certificate_thumbprint)")

            async with httpx.AsyncClient() as client:
                response = await client.post(token_url, data=data)
                response.raise_for_status()
                token_data = response.json()
                self._access_token = token_data.get("access_token")

            return self._access_token

        except Exception as e:
            logger.error("SharePointClient: falha na autenticação: %s", str(e))
            raise

    def _get_headers(self) -> Dict[str, str]:
        """Headers HTTP com token de autenticação."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _get_http_client(self) -> Any:
        """Obtém ou cria o cliente HTTP."""
        if self._http_client is None:
            try:
                import httpx

                self._http_client = httpx.AsyncClient(
                    base_url=self.base_url or "",
                    timeout=self.timeout,
                )
                # Autenticar automaticamente
                await self._authenticate()
            except ImportError:
                logger.error("httpx não instalado. Instale com 'pip install httpx'.")
                raise
        return self._http_client

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca no SharePoint via Graph API.

        Args:
            query: Query de busca.
            **kwargs: Parâmetros adicionais:
                - path: Caminho específico para buscar (ex: /sites/team/Documents)
                - extension: Filtrar por extensão de arquivo (ex: 'pdf', 'docx')

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            logger.debug("SharePointClient: busca ignorada (desativado).")
            return []

        # Cache check
        cache_key = f"sharepoint:search:{query}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            # Garantir token válido
            if not self._access_token:
                await self._authenticate()

            client = await self._get_http_client()

            # Endpoint de busca do Graph API
            endpoint = "/search/query"

            search_params = {
                "querytext": query,
                "count": kwargs.get("limit", self.max_results),
            }

            response = await client.get(endpoint, headers=self._get_headers(), params=search_params)
            response.raise_for_status()
            data = response.json()

            results = self._parse_results(data)

            await self._cache_set(cache_key, results)

            logger.info("SharePointClient: %d resultados para '%s'", len(results), query)
            return results

        except Exception as e:
            logger.warning("SharePointClient: falha na busca '%s': %s", query, str(e))
            return []

    def _parse_results(self, data: Dict[str, Any]) -> List[SearchResult]:
        """Parseia resultados da Graph API para SearchResult.

        Args:
            data: Resposta JSON da Graph API.

        Returns:
            List[SearchResult]: Resultados normalizados.
        """
        results: List[SearchResult] = []
        for item in data.get("value", []):
            try:
                result = SearchResult(
                    source="sharepoint",
                    title=item.get("title", "SharePoint Item"),
                    url=item.get("webUrl", ""),
                    description=item.get("description", ""),
                    metrics={
                        "item_type": item.get("itemType"),
                        "created_date": item.get("createdDateTime"),
                        "last_modified": item.get("lastModifiedDateTime"),
                        "size": item.get("size"),
                    },
                    raw=item,
                )
                results.append(result)
            except Exception as e:
                logger.debug("SharePointClient: falha ao parsear resultado: %s", e)
                continue
        return results

    async def search_site_documents(
        self, site_id: str, query: str, **kwargs
    ) -> List[SearchResult]:
        """Busca em documentos de um site específico.

        Args:
            site_id: ID do site SharePoint.
            query: Query de busca.
            **kwargs: Parâmetros adicionais.

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        if not self.enabled:
            return []

        try:
            if not self._access_token:
                await self._authenticate()

            client = await self._get_http_client()
            endpoint = f"/sites/{site_id}/drive/root/search(q:'{query}')"

            response = await client.get(endpoint, headers=self._get_headers())
            response.raise_for_status()

            results: List[SearchResult] = []
            for item in response.json().get("value", []):
                result = SearchResult(
                    source="sharepoint",
                    title=item.get("name", "Document"),
                    url=item.get("webUrl", ""),
                    description=item.get("description", ""),
                    metrics={
                        "file_extension": item.get("file", {}).get("mimeType", ""),
                        "size": item.get("size"),
                    },
                    raw=item,
                )
                results.append(result)
            return results

        except Exception as e:
            logger.warning("SharePointClient: falha na busca site %s: %s", site_id, str(e))
            return []

    async def close(self) -> None:
        """Libera recursos."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        await super().close()


# ── Mock SharePoint Client para testes offline ───────────────────────────────
class MockSharePointClient:
    """Mock leve para testes e ambientes sem credenciais SharePoint."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._closed = False

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        time.sleep(0.01)

        return [
            SearchResult(
                source="sharepoint",
                title=f"Mock SharePoint Doc: {query}",
                url="https://company.sharepoint.com/sites/team/Documents/mock.docx",
                description=f"Mock SharePoint document containing: {query}",
                metrics={"mock": True, "site": "team"},
                raw={"mock": True, "query": query},
            ),
        ]

    async def close(self) -> None:
        self._closed = True

    __aenter__ = __aexit__ = lambda self: None  # type: ignore