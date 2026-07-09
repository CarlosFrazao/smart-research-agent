"""GooglePatentsSearcher — Busca de patentes via scraping com cascata resiliente.

Usa cascata Firecrawl → Spider → Steel → Jina para raspar resultados de
https://patents.google.com/. Requer SRA_PATENTS_ENABLED=true para ativar.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.search.scraping_searcher import ScrapingSearcher, ScrapingConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.google_patents")

GOOGLE_PATENTS_SEARCH_URL = "https://patents.google.com/xhr/query?url=q%3D{query_encoded}&exp=&download=false"


@register_searcher("google_patents", enabled_env="SRA_PATENTS_ENABLED", trusted=False)
class GooglePatentsSearcher(ScrapingSearcher):
    """Searcher de patentes do Google Patents via cascata de scrapers.

    Usa a cascata resiliente (Firecrawl → Spider → Steel → Jina) para raspar
    resultados de busca do Google Patents. Retorna patentes com ID, título,
    abstract, URL, assignee e data de depósito.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        scrapers: Dict[str, Any] | None = None,
        cache: Any = None,
    ):
        # Configuração padrão de scraping
        scraping_config = ScrapingConfig(
            timeout=config.get("timeout", 30.0),
            rate_limit_rps=config.get("rate_limit_rps", 1.0),
            min_content_length=config.get("min_content_length", 100),
            cache_enabled=config.get("cache_enabled", True),
            cache_ttl_seconds=config.get("cache_ttl_seconds", 3600),
        )
        super().__init__(config, scrapers=scrapers, cache=cache)
        self.scraping_config = scraping_config

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """Executa busca de patentes no Google Patents.

        Args:
            query: Termo de busca (ex: "machine learning", "patent 123456").
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com patentes encontradas.
        """
        import urllib.parse

        encoded_query = urllib.parse.quote_plus(query)
        url = GOOGLE_PATENTS_SEARCH_URL.format(query_encoded=encoded_query)

        try:
            raw = await self._scrape_url(url)
            results = self._parse_patents(raw, query)
            logger.debug(f"GooglePatentsSearcher: {len(results)} patentes para '{query}'")
            return results[: self.max_results]
        except Exception as e:
            logger.warning(f"GooglePatentsSearcher falhou para '{query}': {e}")
            return []

    def _parse_patents(self, raw: Dict[str, Any], query: str) -> List[SearchResult]:
        """Parseia o resultado do scraping para extrair patentes."""
        results: List[SearchResult] = []

        # Se o resultado contém dados estruturados (via Firecrawl/Spider)
        if isinstance(raw, dict) and raw.get("metadata"):
            meta = raw["metadata"]
            if isinstance(meta, dict):
                # Tenta extrair da metadata estruturada
                patent_data = self._extract_from_metadata(meta)
                if patent_data:
                    results.append(self.normalize(patent_data))

        # Fallback: parsing do markdown/conteúdo bruto
        if not results:
            content = raw.get("markdown", raw.get("content", "")) or ""
            if content:
                results.extend(self._parse_from_content(content, query))

        return results

    def _extract_from_metadata(self, meta: Dict[str, Any]) -> Dict[str, Any] | None:
        """Tenta extrair dados de patente de metadata estruturada."""
        # Procura por campos comuns de patentes
        patent_id = meta.get("patent_id") or meta.get("patentId") or meta.get("publicationNumber")
        title = meta.get("title") or meta.get("patentTitle")
        abstract = meta.get("abstract") or meta.get("patentAbstract")
        url = meta.get("url") or meta.get("patentUrl")
        assignee = meta.get("assignee") or meta.get("assigneeName")
        filing_date = meta.get("filing_date") or meta.get("filingDate")

        if patent_id and title:
            return {
                "patent_id": patent_id,
                "title": title,
                "abstract": abstract or "",
                "url": url or "",
                "assignee": assignee or "",
                "filing_date": filing_date or "",
            }
        return None

    def _parse_from_content(self, content: str, query: str) -> List[SearchResult]:
        """Fallback: parsing heurístico do conteúdo markdown/HTML."""
        results: List[SearchResult] = []

        # Busca por padrões comuns em resultados do Google Patents
        # Formato típico: US1234567B2 - Título da Patente
        import re

        # Padrão para IDs de patentes (US, EP, WO, CN, etc.)
        patent_pattern = r"([A-Z]{2}\d+[A-Z]\d*)\s*[-–]\s*([^\n]+)"
        matches = re.findall(patent_pattern, content)

        for patent_id, title in matches[:10]:
            if not title.strip():
                continue
            result = self.normalize({
                "patent_id": patent_id,
                "title": title.strip(),
                "abstract": "",
                "url": f"https://patents.google.com/patent/{patent_id}/en",
                "assignee": "",
                "filing_date": "",
            })
            results.append(result)

        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto de patente para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(
                source="google_patents",
                title="Resultado inválido",
                url="",
                description="",
            )

        patent_id = raw_result.get("patent_id", "")
        title = raw_result.get("title", "")
        abstract = raw_result.get("abstract", "")
        url = raw_result.get("url", "")
        assignee = raw_result.get("assignee", "")
        filing_date = raw_result.get("filing_date", "")

        if not url and patent_id:
            url = f"https://patents.google.com/patent/{patent_id}/en"

        # Monta descrição rica
        parts = []
        if abstract:
            parts.append(abstract[:300])
        if assignee:
            parts.append(f"Assignee: {assignee}")
        if filing_date:
            parts.append(f"Filing: {filing_date}")

        description = " | ".join(parts)

        return SearchResult(
            source="google_patents",
            title=title,
            url=url,
            description=description,
            metrics={
                "patent_id": patent_id,
                "assignee": assignee,
                "filing_date": filing_date,
            },
            raw=raw_result,
        )