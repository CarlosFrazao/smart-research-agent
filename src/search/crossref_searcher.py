"""
crossref_searcher.py — Buscador de citações e metadados acadêmicos via CrossRef REST API

Endpoint: https://api.crossref.org/works?query={query}&rows={n}&mailto={mailto}
Autenticação: nenhuma (endpoint público). CrossRef exige um User-Agent *polite*
incluindo um endereço de e-mail de contato (mailto) para bom comportamento.
Fallback: WebSearcher quando a API retorna < 2 resultados (padrão GAP1/PubMed).

Formato de resposta real (JSON puro em ``response``):
    {"message": {"items": [ { "DOI": ..., "title": [...], "author": [...],
       "issued": {"date-parts": [[ano, mes, dia]]}, "is-referenced-by-count": N,
       "URL": ..., "type": ... }, ... ]}}

Nota de compatibilidade de HTTPClient: o ``HTTPClient`` atual retorna o JSON
puro do corpo (``resp.json()``) quando ``Content-Type`` é ``application/json``.
Mantemos o extrator tolerante: aceita tanto o dict puro quanto o wrapper legado
``{"json": {...}}`` usado por searchers mais antigos, para não replicar o bug
latente de leitura do PubMed/semantic_scholar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://api.crossref.org/works"
# Endereço de contato polite exigido pela CrossRef (mailto no User-Agent/params).
_CROSSREF_MAILTO = "sra@research.local"
_POLITE_USER_AGENT = f"SRA/7.0 (mailto:{_CROSSREF_MAILTO})"


class CrossRefSearcher(BaseSearcher):
    """Buscador de literatura científica e citações via CrossRef REST API.

    Sem API key — endpoint público. Utiliza ``User-Agent`` polite com mailto
    (requisito da CrossRef) e Circuit Breaker para isolar falhas de rede.
    """

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.http = HTTPClient(timeout=self.timeout)
        self.mailto = config.get("crossref_mailto", _CROSSREF_MAILTO)
        self.web_fallback = None  # Injetado pelo Orchestrator se disponível
        self.circuit = CircuitBreakerRegistry.get(
            "crossref_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por citações/metadados no CrossRef.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "crossref_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._do_search, query)
        except CircuitBreakerOpen as e:
            logger.warning(f"CrossRefSearcher: {e}")
            return self.fallback(query)
        except Exception as e:
            logger.error(f"CrossRef search erro: {e}")
            return self.fallback(query)

    @staticmethod
    def _extract_json(response: Any) -> dict[str, Any]:
        """Extrai o payload JSON da resposta do HTTPClient de forma tolerante.

        Aceita o JSON puro retornado pelo ``HTTPClient`` atual (``resp.json()``)
        e o wrapper legado ``{"json": {...}}`` de searchers mais antigos.

        Args:
            response (Any): Dicionário devolvido por ``HTTPClient.get``.

        Returns:
            dict[str, Any]: Payload JSON (pode ser ``{}`` se ausente/inválido).
        """
        if not isinstance(response, dict):
            return {}
        payload = response.get("json")
        if isinstance(payload, dict):
            return payload
        # JSON puro (formato atual do HTTPClient)
        return response

    async def _do_search(self, query: str) -> list[SearchResult]:
        """Executa a chamada HTTP real ao CrossRef, protegida pelo circuit breaker."""
        params = {
            "query": query,
            "rows": min(self.max_results, 20),
            "mailto": self.mailto,
        }
        headers = {"User-Agent": _POLITE_USER_AGENT}

        try:
            response = await self.http.get(_API_BASE, params=params, headers=headers)
            data = self._extract_json(response)
            items = (
                data.get("message", {}).get("items", [])
                if isinstance(data, dict)
                else []
            )

            results = [self._normalize_item(item) for item in items if item]
            results = [r for r in results if r is not None]

            logger.info(
                f"CrossRefSearcher: {len(results)} trabalhos para '{query[:50]}'"
            )

            # Fallback (GAP1): poucos resultados nativos → WebSearcher
            if (
                len(results) < 2
                and self.web_fallback
                and getattr(self.web_fallback, "enabled", False)
            ):
                logger.info("CrossRefSearcher: fallback para WebSearcher ativado.")
                try:
                    web_results = await self.web_fallback.search(
                        f"academic paper {query}"
                    )
                    results.extend(web_results[:5])
                except Exception as e:
                    logger.warning(f"CrossRef WebFallback falhou: {e}")

            return results[: self.max_results]

        except Exception as e:
            logger.error(f"CrossRef API erro: {e}")
            return self.fallback(query)

    def _normalize_item(self, item: dict[str, Any]) -> SearchResult | None:
        """Normaliza um item bruto do CrossRef para o formato ``SearchResult``.

        Args:
            item (dict[str, Any]): Item ``message.items[i]`` da resposta da API.

        Returns:
            SearchResult | None: Resultado normalizado, ou ``None`` se o item
            não tiver título/DOI/URL mínimos.
        """
        try:
            doi = item.get("DOI", "")
            url = item.get("URL", "") or (f"https://doi.org/{doi}" if doi else "")
            title_list = item.get("title") or [""]
            title = title_list[0] if isinstance(title_list, list) else str(title_list)

            if not title and not doi:
                return None

            authors = item.get("author", []) or []
            author_str = self._format_authors(authors)

            citations = int(item.get("is-referenced-by-count", 0) or 0)

            published_at = self._parse_date_parts(item.get("issued"))

            description_parts: list[str] = []
            if author_str:
                description_parts.append(f"Autores: {author_str}.")
            if doi:
                description_parts.append(f"DOI: {doi}.")
            if citations:
                description_parts.append(f"Citações: {citations}.")
            if published_at is not None:
                description_parts.append(f"Publicado em {published_at.year}.")
            work_type = item.get("type", "")
            if work_type:
                description_parts.append(f"Tipo: {work_type}.")

            return SearchResult(
                source="crossref",
                title=title,
                url=url,
                description=" ".join(description_parts),
                published_at=published_at,
                metrics={
                    "doi": doi,
                    "citations": citations,
                    "type": work_type,
                    "authors": author_str,
                },
                raw=item,
            )
        except Exception as e:
            logger.warning(f"CrossRefSearcher: falha ao normalizar item: {e}")
            return None

    @staticmethod
    def _format_authors(authors: list[dict[str, Any]]) -> str:
        """Formata a lista de autores como 'Given Family, Given Family et al.'."""
        names = []
        for a in authors[:3]:
            if not isinstance(a, dict):
                continue
            given = a.get("given", "")
            family = a.get("family", "")
            full = " ".join(part for part in (given, family) if part).strip()
            if full:
                names.append(full)
        if not names:
            return ""
        if len(authors) > 3:
            return ", ".join(names) + " et al."
        return ", ".join(names)

    @staticmethod
    def _parse_date_parts(issued: Any) -> datetime | None:
        """Converte ``issued.date-parts`` (ex.: ``[[2023, 5, 14]]``) em datetime UTC.

        Args:
            issued (Any): Objeto ``issued`` do CrossRef.

        Returns:
            datetime | None: Data UTC (primeiro dia do mês/ano se parcial), ou
            ``None`` quando ausente/inválido (freshness recai sobre ``fetched_at``).
        """
        if not isinstance(issued, dict):
            return None
        date_parts = issued.get("date-parts")
        if not isinstance(date_parts, list) or not date_parts:
            return None
        parts = date_parts[0]
        if not isinstance(parts, list) or not parts:
            return None
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, TypeError, IndexError):
            return None

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto para o formato ``SearchResult``.

        Usado pela interface genérica do ``BaseSearcher``; reutiliza o
        normalizador de item único quando aplicável.

        Args:
            raw_result (Any): Resultado bruto (dict do CrossRef ou já SearchResult).

        Returns:
            SearchResult: Objeto padronizado.
        """
        if isinstance(raw_result, SearchResult):
            return raw_result
        normalized = (
            self._normalize_item(raw_result) if isinstance(raw_result, dict) else None
        )
        if normalized is not None:
            return normalized
        return SearchResult(
            source="crossref",
            title=raw_result.get("title", "") if isinstance(raw_result, dict) else "",
            url=raw_result.get("url", "") if isinstance(raw_result, dict) else "",
            description=raw_result.get("description", "")
            if isinstance(raw_result, dict)
            else "",
            raw=raw_result if isinstance(raw_result, dict) else {},
        )
