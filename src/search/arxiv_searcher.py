from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)


class ArxivSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any], firecrawl_client=None):
        super().__init__(config)
        self.base_url = "http://export.arxiv.org/api/query"
        self.http = HTTPClient(timeout=self.timeout)
        self.firecrawl_client = firecrawl_client

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
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
                    ri_results = await self.firecrawl_client.search_research_index(query, limit=10)
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

    def _parse_xml(self, xml_text: str) -> List[SearchResult]:
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
                                "published": published.text if published is not None else "",
                                "authors": [a.text for a in authors if a.text],
                                "primary_category": (
                                    category.get("term", "") if category is not None else ""
                                ),
                            },
                            raw={},
                        )
                    )
        except Exception as e:
            logger.error(f"Erro ao parsear XML do Arxiv: {e}")
        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        return SearchResult(
            source="arxiv",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics={},
            raw=raw_result,
        )

    def _normalize_research_index_result(self, item: Dict[str, Any]) -> SearchResult:
        """Converte resultado do Firecrawl Research Index para SearchResult."""
        return SearchResult(
            source="arxiv_research_index",
            title=item.get("title", ""),
            url=item.get("url", ""),
            description=item.get("description", "") or item.get("markdown", "")[:500],
            metrics={"source_index": "firecrawl_research"},
            raw=item,
        )
