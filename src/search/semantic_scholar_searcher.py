"""
semantic_scholar_searcher.py — Buscador de Papers Científicos via Semantic Scholar API

Endpoint: https://api.semanticscholar.org/graph/v1/paper/search
Rate-limit: 100 req/5min sem API key, 1000 req/5min com key.
Fallback: WebSearcher quando a API retorna < 2 resultados.
"""
import logging
from typing import List, Dict, Any, Optional

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "paperId,title,abstract,year,authors,externalIds,openAccessPdf,citationCount,venue"


class SemanticScholarSearcher(BaseSearcher):
    """
    Buscador de literatura científica via Semantic Scholar Graph API.

    Suporta paginação (offset/limit), injeção de API key via config,
    e fallback automático para WebSearcher quando a API retorna poucos resultados.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.http = HTTPClient(timeout=self.timeout)
        self.api_key: Optional[str] = config.get("semantic_scholar_api_key")
        self.web_fallback = None  # Injetado pelo Orchestrator se disponível

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        params = {
            "query": query,
            "fields": _FIELDS,
            "limit": min(self.max_results, 100),
            "offset": 0,
        }

        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key

        try:
            response = await self.http.get(_API_BASE, params=params, headers=headers)
            data = response.get("json", {}) or {}
            papers = data.get("data", [])

            results = [self._parse_paper(p) for p in papers if p]
            results = [r for r in results if r is not None]

            logger.info(f"SemanticScholar: {len(results)} papers para '{query[:50]}'")

            # Fallback se retornar < 2 resultados
            if len(results) < 2 and self.web_fallback and getattr(self.web_fallback, "enabled", False):
                logger.info("SemanticScholar: fallback para WebSearcher ativado.")
                try:
                    web_results = await self.web_fallback.search(f"research paper {query}")
                    results.extend(web_results[:5])
                except Exception as e:
                    logger.warning(f"SemanticScholar WebFallback falhou: {e}")

            return results[:self.max_results]

        except Exception as e:
            logger.error(f"SemanticScholar search error: {e}")
            return self.fallback(query)

    def _parse_paper(self, paper: Dict) -> Optional[SearchResult]:
        """
        Converte um paper da API Semantic Scholar em SearchResult.
        """
        try:
            paper_id = paper.get("paperId", "")
            title = paper.get("title", "")
            abstract = paper.get("abstract") or ""
            year = paper.get("year")
            venue = paper.get("venue", "")
            citations = paper.get("citationCount", 0)

            authors_raw = paper.get("authors", [])
            authors_str = ", ".join(
                a.get("name", "") for a in authors_raw[:3]
            )
            if len(authors_raw) > 3:
                authors_str += " et al."

            # Constrói URL: prefere openAccessPdf, senão usa página do paper
            pdf_info = paper.get("openAccessPdf") or {}
            url = pdf_info.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"

            # Extrai DOI se disponível
            external_ids = paper.get("externalIds") or {}
            doi = external_ids.get("DOI", "")

            description_parts = []
            if abstract:
                description_parts.append(abstract[:500])
            if year:
                description_parts.append(f"Publicado em {year}.")
            if venue:
                description_parts.append(f"Venue: {venue}.")
            if authors_str:
                description_parts.append(f"Autores: {authors_str}.")
            if citations:
                description_parts.append(f"Citações: {citations}.")
            if doi:
                description_parts.append(f"DOI: {doi}.")

            return SearchResult(
                source="semantic_scholar",
                title=title,
                url=url,
                description=" ".join(description_parts),
                metrics={"citations": citations, "year": year, "doi": doi},
            )
        except Exception as e:
            logger.warning(f"SemanticScholar: falha ao parsear paper: {e}")
            return None

    def normalize(self, raw_result: Any) -> SearchResult:
        """
        Normaliza um resultado bruto para o formato SearchResult.
        """
        return SearchResult(
            source="semantic_scholar",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics=raw_result.get("metrics", {}),
            raw=raw_result,
        )

