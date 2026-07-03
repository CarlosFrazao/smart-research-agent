"""Searcher usando instancia SearXNG auto-hospedada para buscas na web."""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class SearXNGSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do SearXNG."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        # O padrão usa o container SearXNG se rodando de dentro do Docker,
        # ou o mapeamento de loopback se rodando no host.
        self.searxng_url = config.get("searxng_url", "http://127.0.0.1:3023")
        self.engines = config.get("searxng_engines", "google,bing,duckduckgo")
        self.categories = config.get("searxng_categories", "general")

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no SearXNG.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        url = f"{self.searxng_url.rstrip('/')}/search"
        params = {
            "q": query,
            "engines": self.engines,
            "categories": self.categories,
            "format": "json",
        }

        logger.info(
            f"SearXNGSearcher: Consultando '{query[:50]}' no SearXNG em {self.searxng_url}"
        )

        try:
            # Ignoramos proxies do sistema para conexões internas de rede Docker/localhost
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)

                if response.status_code != 200:
                    logger.warning(
                        f"SearXNGSearcher: Erro ao consultar SearXNG (HTTP {response.status_code})"
                    )
                    return self.fallback(query)

                data = response.json()
                results = data.get("results", [])
                logger.info(
                    f"SearXNGSearcher: {len(results)} resultados brutos retornados."
                )

                # Limita a quantidade máxima configurada de resultados
                results = results[: self.max_results]
                return [self.normalize(r) for r in results]

        except Exception as e:
            logger.error(f"SearXNGSearcher: Falha ao executar busca no SearXNG: {e}")
            return self.fallback(query)

    def normalize(self, raw_result: dict[str, Any]) -> SearchResult:
        """Normaliza um resultado bruto vindo do SearXNG para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        url = raw_result.get("url", "")
        parsed_url = urlparse(url)
        domain = parsed_url.netloc if url else ""

        # Opcional: extrair a pontuação de relevância do SearXNG se fornecida
        score = raw_result.get("score", 0.0)

        return SearchResult(
            source="searxng",
            title=raw_result.get("title", "Sem título"),
            url=url,
            description=raw_result.get("content", raw_result.get("description", "")),
            metrics={
                "source_domain": domain,
                "searxng_score": score,
                "engines": raw_result.get("engines", []),
            },
            raw=raw_result,
        )
