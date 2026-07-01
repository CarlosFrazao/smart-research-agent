"""
JinaSearcher — fallback de scraping zero-config usando Jina Reader (r.jina.ai).

Usado em host_mode=True quando o Firecrawl (Docker) não está disponível.
Não requer chave de API — faz requisições HTTP simples ao endpoint público.
"""

import logging
from typing import Any, Dict, List

import httpx

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class JinaSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get("jina_base_url", "https://r.jina.ai/").rstrip("/")

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        """
        Extrai conteúdo de uma URL via Jina Reader.

        Interpreta `query` como URL direta. Se não for URL, retorna lista vazia
        pois Jina Reader não tem motor de busca próprio.
        """
        if not query.startswith("http"):
            logger.debug(f"JinaSearcher: '{query[:50]}' não é URL, ignorando")
            return self.fallback(query)

        jina_url = f"{self.base_url}/{query}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    jina_url,
                    headers={"Accept": "text/markdown", "X-Return-Format": "markdown"},
                    follow_redirects=True,
                )
                resp.raise_for_status()
                content = resp.text[:2000]
                return [self.normalize({"url": query, "content": content})]
        except Exception as e:
            logger.warning(f"JinaSearcher erro para '{query[:50]}': {e}")
            return self.fallback(query)

    def normalize(self, raw_result: Any) -> SearchResult:
        if isinstance(raw_result, dict):
            content = raw_result.get("content", "")
            url = raw_result.get("url", "")
            title = content.split("\n")[0].lstrip("# ").strip()[:120] if content else url
            return SearchResult(
                source="jina_reader",
                title=title or url,
                url=url,
                description=content[:300],
                metrics={},
                raw=raw_result,
            )
        return SearchResult(
            source="jina_reader",
            title="",
            url="",
            description=str(raw_result)[:300],
            metrics={},
            raw={},
        )
