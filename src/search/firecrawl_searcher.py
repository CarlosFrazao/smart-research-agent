"""Searcher usando Firecrawl para scraping e indexacao de paginas web."""

import logging
from typing import Any

from src.clients.firecrawl_client import FirecrawlClient
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class FirecrawlSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Firecrawl."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.client = FirecrawlClient(
            api_key=config.get("firecrawl_api_key", ""),
            base_url=config.get("firecrawl_base_url"),
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Firecrawl.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        try:
            raw_results = await self.client.search(query, limit=self.max_results)
            return [self.normalize(r) for r in raw_results]
        except Exception as e:
            logger.error(f"FirecrawlSearcher erro: {e}")
            return self.fallback(query)

    def normalize(self, result: dict) -> SearchResult:
        """Normaliza um resultado bruto vindo do Firecrawl para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        return SearchResult(
            source="firecrawl",
            title=result.get("title", ""),
            url=result.get("url", ""),
            description=result.get("markdown", "")[:300],
            metrics={},
            raw=result,
        )
