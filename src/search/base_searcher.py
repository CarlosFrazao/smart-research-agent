"""Classe base abstrata para todos os searchers do Smart Research Agent.

Define a interface padrao que todos os searchers devem implementar:
busca assincrona, normalizacao de resultados e fallback em caso de falha.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from src.types import SearchResult

logger = logging.getLogger(__name__)


class BaseSearcher(ABC):
    """Classe base abstrata para searchers de fontes de dados.

    Define o contrato de interface que todos os searchers devem implementar.
    Fornece atributos comuns (timeout, max_results, enabled) e fallback padrao.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.timeout = config.get("timeout", 30)
        self.max_results = config.get("max_results", 20)
        self.enabled = config.get("enabled", True)

    @abstractmethod
    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa a busca na fonte de dados e retorna resultados normalizados.

        Args:
            query: Texto da query a buscar.
            **kwargs: Parametros extras especificos do searcher.

        Returns:
            list[SearchResult]: Lista de resultados normalizados.
        """

    @abstractmethod
    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto da API para o formato `SearchResult`.

        Args:
            raw_result: Resultado bruto retornado pela API de origem.

        Returns:
            SearchResult: Resultado normalizado no formato padrao do SRA.
        """

    def fallback(self, query: str) -> list[SearchResult]:
        """Retorna lista vazia e registra aviso de fallback ativado.

        Args:
            query: Query que nao pôde ser executada.

        Returns:
            list[SearchResult]: Lista vazia (fallback padrao).
        """
        logger.warning(f"Fallback ativado para {self.__class__.__name__}: {query[:50]}")
        return []

    async def close(self) -> None:
        """Fecha os recursos e conexões abertas do searcher."""
        if hasattr(self, "http") and self.http:
            try:
                await self.http.close()
            except Exception:
                pass
