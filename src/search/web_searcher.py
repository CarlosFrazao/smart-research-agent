"""Searcher de busca geral na web usando Jina Reader e DuckDuckGo como fallback."""

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from src.clients.firecrawl_client import FirecrawlClient
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class WebSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Web."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.firecrawl = FirecrawlClient(
            api_key=config.get("firecrawl_api_key", ""),
            base_url=config.get("firecrawl_base_url"),
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Web.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        # Tentativa 1: Busca Padrão com Stealth
        try:
            logger.info(
                f"WebSearcher: Executando busca com Firecrawl para '{query[:50]}'"
            )
            results = await self.firecrawl.search(
                query, limit=self.max_results, stealth=True
            )
            if results:
                logger.info(
                    f"WebSearcher: {len(results)} resultados obtidos na primeira tentativa."
                )
                return [self.normalize(r) for r in results]
        except Exception as e:
            logger.warning(
                f"Busca padrão do Firecrawl falhou: {e}. Iniciando fallbacks..."
            )

        # Tentativa 2: Simplificar a query para termos essenciais (evitar travar no WAF/Google Filter)
        try:
            words = re.findall(r"\w+", query)
            simplified_query = " ".join(words[:4])
            if simplified_query and simplified_query != query:
                logger.info(
                    f"WebSearcher: Tentando busca simplificada: '{simplified_query}'"
                )
                results = await self.firecrawl.search(
                    simplified_query, limit=self.max_results, stealth=True
                )
                if results:
                    logger.info(
                        f"WebSearcher: {len(results)} resultados obtidos com query simplificada."
                    )
                    return [self.normalize(r) for r in results]
        except Exception as e:
            logger.warning(f"Busca simplificada falhou: {e}")

        # Tentativa 3: Se a busca do Firecrawl falhar completamente, tentar buscar via API de scraping do Jina Reader
        try:
            logger.info(
                "WebSearcher: Fallback de busca ativando Jina Reader para extração direta de busca pública."
            )
            jina_search_url = f"https://s.jina.ai/{quote(query, safe='')}"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    jina_search_url,
                    headers={"Accept": "text/markdown"},
                    follow_redirects=True,
                )
                if resp.status_code == 200 and resp.text:
                    logger.info(
                        "WebSearcher: Sucesso ao obter busca pública via Jina Search API."
                    )
                    content = resp.text
                    return [
                        SearchResult(
                            source="web",
                            title=f"Busca Jina: {query[:40]}",
                            url=jina_search_url,
                            description=content[:500],
                            metrics={"source_domain": "s.jina.ai"},
                            raw={"markdown": content},
                        )
                    ]
        except Exception as e:
            logger.error(f"Fallback Jina Search falhou: {e}")

        # Retorno de contingência
        logger.error("WebSearcher: Todas as tentativas de busca web falharam.")
        return self.fallback(query)

    def normalize(self, result: dict) -> SearchResult:
        """Normaliza um resultado bruto vindo do Web para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        url = result.get("url", result.get("metadata", {}).get("sourceURL", ""))
        parts = url.split("/")
        domain = parts[2] if url and len(parts) > 2 else ""
        return SearchResult(
            source="web",
            title=result.get("title", result.get("metadata", {}).get("title", "")),
            url=url,
            description=result.get("description", result.get("markdown", "")[:300]),
            metrics={"source_domain": domain},
            raw=result,
        )
