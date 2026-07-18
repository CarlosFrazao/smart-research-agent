"""TavilySearcher — busca + extracao otimizada para LLM via Tavily (tavily.com).

Adaptado de ``Hermes Agent/plugins/web/tavily/provider.py`` (MIT, Nous Research).
Diferencas da fonte:
- Remove o ABC ``WebSearchProvider`` e o dispatcher ``tools.interrupt``/
  ``get_provider_env`` — usa o contrato ``BaseSearcher`` do SRA e ``os.getenv``.
- ``search()`` e assincrono: a chamada HTTP roda via ``self._http_request``
  (httpx.AsyncClient com retry Tenacity herdado da base).
- ``max_results`` e limitado a 20 (teto da API Tavily).
- Resultados de extracao com falha (``failed_results``/``failed_urls``) sao
  descartados pelo ``normalize`` (retorna ``None`` quando nao ha URL valida).

Sem SDK externo: Tavily e uma API REST pura (httpx ja presente no SRA).

Ativacao:
    export TAVILY_API_KEY=...        # obrigatoria
    export SRA_TAVILY_ENABLED=true   # liga o searcher no factory
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.logging_utils import redact_sensitive_text
from src.search.base_searcher import BaseSearcher
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger(__name__)

# Tavily API impoe um limite duro de 20 resultados por busca.
_TAVILY_MAX_RESULTS_CAP = 20
_TAVILY_DEFAULT_BASE_URL = "https://api.tavily.com"


@register_searcher(
    "tavily",
    requires_key="TAVILY_API_KEY",
    enabled_env="SRA_TAVILY_ENABLED",
    trusted=True,
)
class TavilySearcher(BaseSearcher):
    """Busca e extracao de conteudo via Tavily, otimizados para consumo por LLM.

    Retorna trechos de conteudo ja ranqueados e resumidos, reduzindo o numero
    de tokens necessarios no contexto de uma pesquisa profunda (deep research).
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Inicializa o searcher e resolve a API key/base URL do ambiente.

        A ausencia de ``TAVILY_API_KEY`` ou do pacote nao quebra a construcao:
        o factory do SRA so instancia este searcher quando ``TAVILY_API_KEY``
        e ``SRA_TAVILY_ENABLED`` estiverem presentes.

        Args:
            config: Dicionario de configuracao do orquestrador.
            **kwargs: Parametros alternativos quando ``config`` e None.
        """
        super().__init__(config, **kwargs)
        self._api_key: str | None = os.getenv("TAVILY_API_KEY")
        self._base_url: str = (
            os.getenv("TAVILY_BASE_URL") or _TAVILY_DEFAULT_BASE_URL
        ).rstrip("/")

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """Executa busca no Tavily e normaliza os resultados.

        Args:
            query: Texto da busca.
            **kwargs: Parametros extras ignorados (compatibilidade de interface).

        Returns:
            list[SearchResult]: Resultados normalizados (itens sem URL sao
            descartados). Em caso de falha (sem API key, erro de rede) retorna
            lista vazia via ``fallback`` e registra o erro.
        """
        if not self._api_key:
            logger.warning("Tavily indisponivel: TAVILY_API_KEY nao configurada")
            return self.fallback(query)

        capped_limit = min(self.max_results, _TAVILY_MAX_RESULTS_CAP)
        payload = {
            "query": query,
            "max_results": capped_limit,
            "include_raw_content": False,
            "include_images": False,
            "api_key": self._api_key,
        }
        try:
            logger.info(
                "Tavily search: '%s' (limit=%d)",
                redact_sensitive_text(query)[:80],
                capped_limit,
            )
            response = await self._http_request(
                "POST", f"{self._base_url}/search", json_body=payload
            )
            data = response.json()
            results = data.get("results", []) or []
            normalized = [self.normalize(r) for r in results]
            # Descarta itens invalidos (normalize retornou None).
            return [n for n in normalized if n is not None]
        except Exception as exc:  # superficie como fallback controlado
            logger.warning("Tavily search error: %s", exc)
            return self.fallback(query)

    def normalize(self, raw_result: Any) -> SearchResult | None:
        """Normaliza um resultado bruto do Tavily para ``SearchResult``.

        O ``raw_result`` e um dict da API Tavily com ``title``, ``url`` e
        ``content``. Itens sem ``url`` (ex.: entradas de falha de extracao)
        sao descartados retornando ``None``.

        Args:
            raw_result: Dict resultado retornado pela API Tavily.

        Returns:
            SearchResult | None: Resultado padronizado com ``source="tavily"``,
            ou ``None`` quando o item nao tem URL valida.
        """
        if not isinstance(raw_result, dict):
            return None

        url = raw_result.get("url", "") or ""
        if not url:
            return None

        title = raw_result.get("title", "") or ""
        content = raw_result.get("content", "") or ""
        description = content[:300]

        return SearchResult(
            source="tavily",
            title=title,
            url=url,
            description=description,
            metrics={},
            raw=raw_result,
        )
