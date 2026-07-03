import logging
import xml.etree.ElementTree as ET
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient
from src.utils.retry import RetryConfig, with_retry

logger = logging.getLogger(__name__)

_RETRY_CONFIG = RetryConfig(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    exponential_base=2.0,
    jitter=True,
    retryable_exceptions=(Exception,),
)


class ArxivSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Arxiv."""

    def __init__(self, config: dict[str, Any], firecrawl_client=None):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.base_url = "http://export.arxiv.org/api/query"
        self.http = HTTPClient(timeout=self.timeout)
        self.firecrawl_client = firecrawl_client
        self.circuit = CircuitBreakerRegistry.get(
            "arxiv_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no arXiv.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "arxiv_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._do_search, query)
        except CircuitBreakerOpen as e:
            logger.warning(f"ArxivSearcher: {e}")
            return self.fallback(query)

    @with_retry(_RETRY_CONFIG)
    async def _do_search(self, query: str) -> list[SearchResult]:
        """Executa a chamada HTTP/API interna para pesquisar no arXiv sem tratamento de falhas.

        Args:
            query (str): Termo de busca a ser pesquisado.

        Returns:
            list[SearchResult]: Resultados brutos ou pré-processados da busca.
        """
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(self.max_results, 50),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

        try:
            data = await self.http.get(self.base_url, params=params)
            text = data.get("text", "")
            results = self._parse_xml(text)

            # Fallback para o Research Index do Firecrawl se resultados nativos < 3
            if len(results) < 3 and self.firecrawl_client:
                logger.info(
                    f"ArxivSearcher: apenas {len(results)} resultados nativos. "
                    f"Acionando Firecrawl Research Index para '{query}'..."
                )
                try:
                    ri_results = await self.firecrawl_client.search_research_index(
                        query, limit=10
                    )
                    seen_urls = {r.url for r in results}
                    for item in ri_results:
                        normalized = self._normalize_research_index_result(item)
                        if normalized.url and normalized.url not in seen_urls:
                            results.append(normalized)
                            seen_urls.add(normalized.url)
                except Exception as ri_err:
                    logger.warning(f"Firecrawl Research Index falhou: {ri_err}")

            return results
        except Exception as e:
            logger.error(f"Arxiv search erro: {e}")
            return self.fallback(query)

    def _parse_xml(self, xml_text: str) -> list[SearchResult]:
        results = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("atom:entry", ns):
                title = entry.find("atom:title", ns)
                summary = entry.find("atom:summary", ns)
                link = entry.find("atom:link[@rel='alternate']", ns)
                published = entry.find("atom:published", ns)
                authors = entry.findall("atom:author/atom:name", ns)
                category = entry.find("atom:category", ns)

                if title is not None and link is not None:
                    results.append(
                        SearchResult(
                            source="arxiv",
                            title=(title.text or "").strip(),
                            url=link.get("href", ""),
                            description=(
                                summary.text[:500]
                                if summary is not None and summary.text
                                else ""
                            ),
                            metrics={
                                "published": published.text
                                if published is not None
                                else "",
                                "authors": [a.text for a in authors if a.text],
                                "primary_category": (
                                    category.get("term", "")
                                    if category is not None
                                    else ""
                                ),
                            },
                            raw={},
                        )
                    )
        except Exception as e:
            logger.error(f"Erro ao parsear XML do Arxiv: {e}")
        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto vindo do arXiv para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        return SearchResult(
            source="arxiv",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics={},
            raw=raw_result,
        )

    def _normalize_research_index_result(self, item: dict[str, Any]) -> SearchResult:
        """Converte resultado do Firecrawl Research Index para SearchResult."""
        return SearchResult(
            source="arxiv_research_index",
            title=item.get("title", ""),
            url=item.get("url", ""),
            description=item.get("description", "") or item.get("markdown", "")[:500],
            metrics={"source_index": "firecrawl_research"},
            raw=item,
        )
