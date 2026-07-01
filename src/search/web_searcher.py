from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.clients.firecrawl_client import FirecrawlClient
import logging
import re
import httpx

logger = logging.getLogger(__name__)


class WebSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.firecrawl = FirecrawlClient(
            api_key=config.get("firecrawl_api_key", ""),
            base_url=config.get("firecrawl_base_url"),
        )

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        # Tentativa 1: Busca Padrão com Stealth
        try:
            logger.info(f"WebSearcher: Executando busca com Firecrawl para '{query[:50]}'")
            results = await self.firecrawl.search(query, limit=self.max_results, stealth=True)
            if results:
                logger.info(f"WebSearcher: {len(results)} resultados obtidos na primeira tentativa.")
                return [self.normalize(r) for r in results]
        except Exception as e:
            logger.warning(f"Busca padrão do Firecrawl falhou: {e}. Iniciando fallbacks...")

        # Tentativa 2: Simplificar a query para termos essenciais (evitar travar no WAF/Google Filter)
        try:
            words = re.findall(r"\w+", query)
            simplified_query = " ".join(words[:4])
            if simplified_query and simplified_query != query:
                logger.info(f"WebSearcher: Tentando busca simplificada: '{simplified_query}'")
                results = await self.firecrawl.search(simplified_query, limit=self.max_results, stealth=True)
                if results:
                    logger.info(f"WebSearcher: {len(results)} resultados obtidos com query simplificada.")
                    return [self.normalize(r) for r in results]
        except Exception as e:
            logger.warning(f"Busca simplificada falhou: {e}")

        # Tentativa 3: Se a busca do Firecrawl falhar completamente, tentar buscar via API de scraping do Jina Reader
        try:
            logger.info(f"WebSearcher: Fallback de busca ativando Jina Reader para extração direta de busca pública.")
            jina_search_url = f"https://s.jina.ai/{query}"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    jina_search_url,
                    headers={"Accept": "text/markdown"},
                    follow_redirects=True
                )
                if resp.status_code == 200 and resp.text:
                    logger.info("WebSearcher: Sucesso ao obter busca pública via Jina Search API.")
                    content = resp.text
                    return [SearchResult(
                        source="web",
                        title=f"Busca Jina: {query[:40]}",
                        url=jina_search_url,
                        description=content[:500],
                        metrics={"source_domain": "s.jina.ai"},
                        raw={"markdown": content}
                    )]
        except Exception as e:
            logger.error(f"Fallback Jina Search falhou: {e}")

        # Retorno de contingência
        logger.error("WebSearcher: Todas as tentativas de busca web falharam.")
        return self.fallback(query)

    def normalize(self, result: dict) -> SearchResult:
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

