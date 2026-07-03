"""Searcher de produtos e ferramentas no Product Hunt via GraphQL API."""

import logging
from typing import Any

import aiohttp

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)

_GRAPHQL_QUERY = (
    "query SearchPosts($query: String!, $first: Int!) { "
    "posts(first: $first, search: $query) { "
    "edges { node { id name tagline url votesCount commentsCount createdAt topics { name } } } "
    "} }"
)


class ProductHuntSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do ProductHunt."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.token = config.get("producthunt_token")
        self.base_url = "https://api.producthunt.com/v2/api/graphql"

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Producthunt.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        if not self.token:
            logger.warning("ProductHunt token nao configurado. Pulando.")
            return self.fallback(query)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": _GRAPHQL_QUERY,
            "variables": {"query": query, "first": min(self.max_results, 20)},
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    data = await resp.json()
                    edges = data.get("data", {}).get("posts", {}).get("edges", [])
                    return [self.normalize(edge["node"]) for edge in edges]
        except Exception as e:
            logger.error(f"ProductHunt search erro: {e}")
            return self.fallback(query)

    def normalize(self, node: dict) -> SearchResult:
        """Normaliza um resultado bruto vindo do Producthunt para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        topics = [t.get("name", "") for t in node.get("topics", [])]
        return SearchResult(
            source="producthunt",
            title=node.get("name", ""),
            url=node.get("url", ""),
            description=node.get("tagline", ""),
            metrics={
                "votes": node.get("votesCount", 0),
                "comments": node.get("commentsCount", 0),
                "created_at": node.get("createdAt", ""),
                "topics": topics,
            },
            raw=node,
        )
