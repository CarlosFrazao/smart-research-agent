"""
JinaSearcher — fallback de scraping zero-config usando Jina Reader (r.jina.ai).

Usado em host_mode=True quando o Firecrawl (Docker) não está disponível.
Não requer chave de API — faz requisições HTTP simples ao endpoint público.
"""

import logging
from typing import Any

import httpx

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


class JinaSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Jina."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.base_url = config.get("jina_base_url", "https://r.jina.ai/").rstrip("/")

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
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
        """Normaliza um resultado bruto vindo do Jina para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        if isinstance(raw_result, dict):
            content = raw_result.get("content", "")
            url = raw_result.get("url", "")
            title = (
                content.split("\n")[0].lstrip("# ").strip()[:120] if content else url
            )
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
