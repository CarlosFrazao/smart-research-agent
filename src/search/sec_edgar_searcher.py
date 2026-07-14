"""SECEdgarSearcher — Busca de filings na SEC via EDGAR Full-Text Search API.

Endpoint: GET https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt=2020-01-01&forms=10-K,8-K

A SEC exige o header ``User-Agent`` com informações de contato. O searcher
usa o User-Agent obrigatório: "smart-research-agent info@example.com".
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.sec_edgar")

SEC_EDGAR_BASE_URL = "https://efts.sec.gov"
SEC_EDGAR_USER_AGENT = "smart-research-agent info@example.com"


@register_searcher("sec_edgar", enabled_env="SRA_EDGAR_ENABLED")
class SECEdgarSearcher(APISearcher):
    """Searcher de filings corporativos da SEC via EDGAR API.

    Busca documentos SEC (10-K, 8-K) por termo de busca. Requer User-Agent
    conforme política da SEC.
    """

    def __init__(self, config: dict[str, Any]):
        user_agent = config.get("sec_edgar_user_agent", SEC_EDGAR_USER_AGENT)
        api_config = APISearcherConfig(
            source_name="sec_edgar",
            base_url=SEC_EDGAR_BASE_URL,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
            default_headers={"User-Agent": user_agent},
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca de filings na SEC EDGAR.

        Args:
            query: Termo de busca (empresa, ticker, palavra-chave).
            **kwargs: Parâmetros adicionais (ignorados).

        Returns:
            Lista de SearchResult com filings encontrados.
        """
        params = {
            "q": query,
            "dateRange": "custom",
            "startdt": "2020-01-01",
            "forms": "10-K,8-K",
        }

        try:
            data = await self._make_request(
                "GET", "/LATEST/search-index", params=params
            )
            hits = data.get("hits", {}) if isinstance(data, dict) else {}
            hits_list = hits.get("hits", []) if isinstance(hits, dict) else []
            parsed = [self.normalize(r) for r in hits_list if isinstance(r, dict)]
            logger.debug(f"SECEdgarSearcher: {len(parsed)} filings para '{query}'")
            return parsed[: self.max_results]
        except Exception as e:
            logger.warning(f"SECEdgarSearcher falhou para '{query}': {e}")
            return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um filing da SEC para SearchResult."""
        if not isinstance(raw_result, dict):
            return SearchResult(source="sec_edgar", title="", url="", description="")

        source_data = raw_result.get("_source", {}) or {}
        filing_id = raw_result.get("_id", "")
        display_names = source_data.get("display_names", [])

        if isinstance(display_names, list):
            display_names = ", ".join(str(d) for d in display_names)

        url = self._build_filing_url(filing_id)

        return SearchResult(
            source="sec_edgar",
            title=str(display_names),
            url=url,
            description=f"Period: {source_data.get('period_of_report', '')}",
            metrics={
                "display_names": display_names,
                "file_date": source_data.get("file_date", ""),
                "period_of_report": source_data.get("period_of_report", ""),
                "filing_id": filing_id,
            },
            raw=raw_result,
        )

    @staticmethod
    def _build_filing_url(filing_id: str) -> str:
        """Constrói URL do filing a partir do ID da busca."""
        if not filing_id:
            return ""
        # ID format: CIK-ACCESSION-DATE (ex: 0000320193-20-000123)
        parts = filing_id.split("-")
        if len(parts) >= 3:
            cik = parts[0].lstrip("0") or parts[0]
            accession_nodash = filing_id.replace("-", "")
            return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{filing_id}.txt"
        return f"https://www.sec.gov/Archives/edgar/data/{filing_id}"
