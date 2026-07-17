"""ExaSearcher — busca semantica neural via Exa (exa.ai).

Adaptado de ``Hermes Agent/plugins/web/exa/provider.py`` (MIT, Nous Research).
Diferencas da fonte:
- Remove o ABC ``WebSearchProvider`` e o dispatcher ``tools.interrupt``/
  ``get_provider_env`` — usa o contrato ``BaseSearcher`` do SRA e ``os.getenv``.
- ``search()`` e assincrono: o SDK oficial ``exa-py`` e sync-only, entao a
  chamada bloqueante roda em ``asyncio.to_thread`` para nao travar o loop.
- Mapeia os resultados para o modelo ``SearchResult`` padrao do SRA.

A dependencia ``exa-py`` e lazy-importada (so carregada quando ha uma busca
real e a API key esta presente), portanto o boot do SRA nao exige o pacote.

Ativacao:
    export EXA_API_KEY=...            # obrigatoria
    export SRA_EXA_ENABLED=true       # liga o searcher no factory
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.logging_utils import redact_sensitive_text
from src.search.base_searcher import BaseSearcher
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger(__name__)


@register_searcher(
    "exa", requires_key="EXA_API_KEY", enabled_env="SRA_EXA_ENABLED", trusted=True
)
class ExaSearcher(BaseSearcher):
    """Busca semantica neural usando a API do Exa.

    Retorna resultados ranqueados por relevancia semantica (nao apenas
    lexical), util para recuperar conteudo conceitualmente proximo da query.
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Inicializa o searcher.

        O cliente Exa e lazy (so criado na primeira busca), entao a ausencia
        de ``EXA_API_KEY`` ou de ``exa-py`` nao quebra a construcao.

        Args:
            config: Dicionario de configuracao do orquestrador.
            **kwargs: Parametros alternativos quando ``config`` e None.
        """
        super().__init__(config, **kwargs)
        self._exa_client: Any | None = None

    def _get_exa_client(self) -> Any:
        """Importa e cacheia o cliente Exa sob demanda.

        Returns:
            Cliente ``exa_py.Exa`` instanciado.

        Raises:
            ValueError: Se ``EXA_API_KEY`` nao estiver configurada.
            ImportError: Se o pacote ``exa-py`` nao estiver instalado.
        """
        if self._exa_client is not None:
            return self._exa_client

        api_key = os.getenv("EXA_API_KEY")
        if not api_key:
            raise ValueError(
                "EXA_API_KEY environment variable not set. "
                "Get your API key at https://exa.ai"
            )

        try:
            from exa_py import Exa  # lazy import — exa-py e opcional no SRA
        except ImportError as exc:  # pragma: no cover - dependencia opcional
            raise ImportError(
                "O pacote 'exa-py' nao esta instalado. Instale com: pip install exa-py"
            ) from exc

        client = Exa(api_key=api_key)
        client.headers["x-exa-integration"] = "smart-research-agent"
        self._exa_client = client
        return client

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """Executa busca semantica no Exa e normaliza os resultados.

        Args:
            query: Texto da busca.
            **kwargs: Parametros extras ignorados (compatibilidade de interface).

        Returns:
            list[SearchResult]: Resultados normalizados. Em caso de falha
            (sem API key, SDK ausente ou erro de rede) retorna lista vazia
            via ``fallback`` e registra o erro.
        """
        try:
            logger.info(
                "Exa search: '%s' (limit=%d)",
                redact_sensitive_text(query)[:80],
                self.max_results,
            )
            # SDK exa-py e sync-only: roda em thread para nao bloquear o loop.
            response = await asyncio.to_thread(
                self._get_exa_client().search,
                query,
                num_results=self.max_results,
                contents={"highlights": True},
            )
            results = getattr(response, "results", None) or []
            return [self.normalize(r) for r in results]
        except (ValueError, ImportError) as exc:
            logger.warning("Exa search indisponivel: %s", exc)
            return self.fallback(query)
        except Exception as exc:  # superficie como fallback controlado
            logger.warning("Exa search error: %s", exc)
            return self.fallback(query)

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto do Exa para ``SearchResult``.

        O ``raw_result`` e um objeto do SDK Exa com atributos ``url``,
        ``title`` e ``highlights`` (lista opcional de trechos relevantes).

        Args:
            raw_result: Objeto resultado retornado pela API Exa.

        Returns:
            SearchResult: Resultado padronizado com ``source="exa"``.
        """
        url = getattr(raw_result, "url", "") or ""
        title = getattr(raw_result, "title", "") or ""
        highlights = getattr(raw_result, "highlights", None)
        if highlights:
            description = " ".join(highlights)
        else:
            description = ""
        return SearchResult(
            source="exa",
            title=title,
            url=url,
            description=description,
            metrics={},
            raw=_to_dict(raw_result),
        )


def _to_dict(raw_result: Any) -> dict[str, Any]:
    """Converte o objeto resultado do Exa em dict para o campo ``raw``.

    O SDK Exa devolve objetos (nem sempre dict serializavel). Preservamos
    os campos relevantes para inspecao/debug sem acoplar ao tipo interno.

    Args:
        raw_result: Objeto resultado do Exa.

    Returns:
        dict: Representacao segura do resultado bruto.
    """
    if isinstance(raw_result, dict):
        return raw_result
    try:
        as_dict = dict(raw_result.__dict__)
    except (TypeError, ValueError):
        as_dict = {}
    as_dict.setdefault("url", getattr(raw_result, "url", ""))
    as_dict.setdefault("title", getattr(raw_result, "title", ""))
    as_dict.setdefault("highlights", getattr(raw_result, "highlights", None))
    return as_dict
