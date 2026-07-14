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
        # Fallback de busca textual (ex.: WebSearcher) usado quando o token do
        # Firecrawl é inválido (401) — ver GAP 1 do PLANO_FECHAR_GAPS.md.
        # Segue o padrão de pubmed_searcher.py / youtube_searcher.py.
        self.web_fallback = None

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
        finally:
            # Cascata resilient: token inválido (401) -> delega ao web_fallback
            if getattr(self.client, "auth_failed", False):
                fallback_results = await self._run_web_fallback(query)
                if fallback_results:
                    return fallback_results

    async def _run_web_fallback(self, query: str) -> list[SearchResult]:
        """Executa busca na web como fallback quando o Firecrawl falha por auth."""
        if self.web_fallback and getattr(self.web_fallback, "enabled", False):
            try:
                logger.info(f"Firecrawl: executando web fallback para '{query[:40]}'")
                return await self.web_fallback.search(f"Firecrawl article {query}")
            except Exception as e:
                logger.warning(f"Firecrawl: falha no web fallback: {e}")
        return []

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
