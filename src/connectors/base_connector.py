"""
Base Connector — Interface base para conectores de fontes externas do SRA.

Este módulo define o contrato que todos os conectores devem implementar,
incluindo Notion, Confluence, SharePoint e futuros conectores internos.

Design:
- Protocol-based (typing.Protocol) para fácil mocking em testes.
- Método `search()` retorna lista de `SearchResult` compatíveis com o pipeline.
- Método `close()` para limpeza de recursos (conexões, tokens, etc).
- Cache opcional integrado via `cache` parameter.
- Suporte a fallback gracioso quando a API está indisponível.

Exemplo de uso:
    connector = NotionClient(api_key=NOTION_KEY)
    results = await connector.search("project documentation")
    # results é List[SearchResult] compatível com SearchStage
"""

from __future__ import annotations

from typing import Any, List, Optional, Protocol

from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("connectors.base")


class BaseConnector(Protocol):
    """Protocolo mínimo para conectores de fontes externas.

    Todos os conectores devem implementar este protocolo para serem
    consumidos pelo `SourcePlanner` e `SearchStage` do pipeline.

    Exemplo de implementação:
        class NotionClient:
            async def search(self, query: str) -> List[SearchResult]:
                ...
            async def close(self) -> None:
                ...
    """

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca na fonte externa.

        Args:
            query: Query de busca.
            **kwargs: Parâmetros adicionais (ex: filtros, paginação).

        Returns:
            List[SearchResult]: Resultados compatíveis com o pipeline.
        """
        ...

    async def close(self) -> None:
        """Libera recursos (conexões, tokens, etc)."""
        ...


class BaseConnectorImplementation:
    """Classe base com implementação parcial para facilitar a extensão.

    Herde desta classe para implementar conectores específicos.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 30,
        cache: Optional[Any] = None,
    ) -> None:
        """Inicializa o conector com configuração.

        Args:
            api_key: Chave de API para a fonte.
            base_url: URL base da API (se diferente do padrão).
            timeout: Timeout de requisição em segundos.
            cache: Instância de Cache para deduplicação.
        """
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.cache = cache
        self._closed = False

        # Validação básica
        if not api_key:
            logger.warning(
                "%s: API key não fornecida. Busca pode falhar. "
                "Configure a variável de ambiente correspondente.",
                self.__class__.__name__,
            )

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca na fonte externa.

        Deve ser sobrescrito pelas subclasses.

        Args:
            query: Query de busca.
            **kwargs: Parâmetros adicionais.

        Returns:
            List[SearchResult]: Resultados da busca.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.search() não implementado"
        )

    async def close(self) -> None:
        """Libera recursos. Chame este método quando o conector não for mais necessário."""
        if self._closed:
            return
        self._closed = True
        logger.debug("%s: recursos liberados.", self.__class__.__name__)

    def _normalize_result(
        self, raw_result: dict[str, Any], source_name: str
    ) -> SearchResult:
        """Normaliza um resultado bruto para SearchResult.

        Args:
            raw_result: Dicionário bruto retornado pela API.
            source_name: Nome da fonte (ex: "notion", "confluence").

        Returns:
            SearchResult: Resultado normalizado.
        """
        title = raw_result.get("title", raw_result.get("name", ""))
        url = raw_result.get("url", raw_result.get("link", ""))
        description = raw_result.get("description", raw_result.get("excerpt", ""))
        metrics = raw_result.get("metrics", {})

        return SearchResult(
            source=source_name,
            title=title,
            url=url,
            description=description,
            metrics=metrics,
            raw=raw_result,
        )

    async def _cache_get(self, key: str) -> Optional[List[SearchResult]]:
        """Obtém resultados do cache se disponível."""
        if self.cache:
            return await self.cache.get(key)
        return None

    async def _cache_set(
        self, key: str, results: List[SearchResult], ttl: int = 300
    ) -> None:
        """Armazena resultados no cache se disponível."""
        if self.cache:
            await self.cache.set(key, results, ttl=ttl)
