from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
import logging
import httpx
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class SearXNGSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # O padrão usa o container SearXNG se rodando de dentro do Docker, 
        # ou o mapeamento de loopback se rodando no host.
        self.searxng_url = config.get("searxng_url", "http://127.0.0.1:3023")
        self.engines = config.get("searxng_engines", "google,bing,duckduckgo")
        self.categories = config.get("searxng_categories", "general")

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        url = f"{self.searxng_url.rstrip('/')}/search"
        params = {
            "q": query,
            "engines": self.engines,
            "categories": self.categories,
            "format": "json"
        }
        
        logger.info(f"SearXNGSearcher: Consultando '{query[:50]}' no SearXNG em {self.searxng_url}")
        
        try:
            # Ignoramos proxies do sistema para conexões internas de rede Docker/localhost
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                
                if response.status_code != 200:
                    logger.warning(f"SearXNGSearcher: Erro ao consultar SearXNG (HTTP {response.status_code})")
                    return self.fallback(query)
                    
                data = response.json()
                results = data.get("results", [])
                logger.info(f"SearXNGSearcher: {len(results)} resultados brutos retornados.")
                
                # Limita a quantidade máxima configurada de resultados
                results = results[:self.max_results]
                return [self.normalize(r) for r in results]
                
        except Exception as e:
            logger.error(f"SearXNGSearcher: Falha ao executar busca no SearXNG: {e}")
            return self.fallback(query)

    def normalize(self, raw_result: Dict[str, Any]) -> SearchResult:
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
                "engines": raw_result.get("engines", [])
            },
            raw=raw_result
        )
