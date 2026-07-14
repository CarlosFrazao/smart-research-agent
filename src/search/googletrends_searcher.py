"""GoogleTrendsSearcher — Interesse relativo no Google Trends.

Utiliza a biblioteca ``pytrends`` (opcional) para obter o interesse relativo
ao longo do tempo (últimas 12 meses) para a query. Se ``pytrends`` não estiver
instalada, o searcher degrada graciosamente e retorna lista vazia.

A fonte é marcada como não-confiável (``trusted=False``) pois os dados vêm de
uma origem externa — passa pelo ``LLMSanitizer`` em ``search_stage.py``.

Nota: ``pytrends`` NÃO é uma dependência do projeto (ver CLAUDE.md). O import é
protegido para que a ausência da lib não quebre o boot do agente.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.google_trends")

GOOGLE_TRENDS_URL = "https://trends.google.com/trends/explore?q="

try:
    from pytrends.request import TrendReq  # type: ignore

    _PYTRENDS_AVAILABLE = True
except ImportError:  # pragma: no cover - depende do ambiente
    TrendReq = None  # type: ignore
    _PYTRENDS_AVAILABLE = False


@register_searcher(
    "google_trends", enabled_env="SRA_GOOGLE_TRENDS_ENABLED", trusted=False
)
class GoogleTrendsSearcher(APISearcher):
    """Searcher para interesse relativo no Google Trends (via pytrends).

    Retorna o interesse relativo ao longo do tempo (12 meses) para a query,
    formatado como um único ``SearchResult`` com pico de popularidade.

    Attributes:
        pytrends: Instância de ``TrendReq`` ou ``None`` se indisponível.
        available: True se ``pytrends`` pôde ser importado e instanciado.
    """

    def __init__(self, config: dict[str, Any]):
        api_config = APISearcherConfig(
            source_name="google_trends",
            base_url="https://trends.google.com",
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 1),
            circuit_config=None,
            cache_ttl=None,  # dados de tendência mudam rapidamente
        )
        super().__init__(api_config)

        self.available = False
        self.pytrends = None
        if _PYTRENDS_AVAILABLE and TrendReq is not None:
            try:
                self.pytrends = TrendReq(
                    hl=config.get("hl", "en-US"),
                    tz=config.get("tz", 360),
                    timeout=(10, 25),
                )
                self.available = True
            except Exception as e:  # pragma: no cover - depende de rede/ambiente
                logger.warning(f"GoogleTrendsSearcher: falha ao iniciar pytrends: {e}")
                self.pytrends = None
                self.available = False

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Obtém o interesse relativo no Google Trends para a query.

        Args:
            query: Termo de busca.
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista com até 1 SearchResult. Vazia se ``pytrends`` indisponível
            ou em caso de erro (degradação graciosa).
        """
        if not self.available or self.pytrends is None:
            logger.debug(
                "GoogleTrendsSearcher: pytrends indisponível — retornando vazio"
            )
            return []

        try:
            summary = await asyncio.to_thread(self._fetch_trends, query)
        except Exception as e:
            logger.warning(f"GoogleTrendsSearcher falhou para '{query}': {e}")
            return []

        if not summary:
            return []

        return [self._build_result(query, summary)]

    def _fetch_trends(self, query: str) -> dict[str, Any] | None:
        """Executa a consulta síncrona ao pytrends (rode em thread).

        Returns:
            Dict com ``peak_interest``, ``avg_interest`` e ``points`` ou None.
        """
        if self.pytrends is None:
            return None

        self.pytrends.build_payload(
            [query],
            timeframe="today 12-m",
            cat=0,
            geo="",
            gprop="",
        )
        interest = self.pytrends.interest_over_time()
        if interest is None or interest.empty:
            return None

        # A coluna da query traz o interesse relativo (0-100); 'isPartial' é lixo
        if query not in interest.columns:
            return None

        series = interest[query].dropna()
        if series.empty:
            return None

        peak = float(series.max())
        avg = float(series.mean())
        points = int(len(series))
        last = float(series.iloc[-1])

        return {
            "peak_interest": round(peak, 1),
            "avg_interest": round(avg, 1),
            "last_interest": round(last, 1),
            "points": points,
        }

    def _build_result(self, query: str, summary: dict[str, Any]) -> SearchResult:
        """Constrói o SearchResult a partir do resumo do pytrends."""
        peak = summary.get("peak_interest", 0.0)
        avg = summary.get("avg_interest", 0.0)
        points = summary.get("points", 0)
        url = f"{GOOGLE_TRENDS_URL}{query.replace(' ', '%20')}"
        description = (
            f"Popularidade: {peak:.0f}% de interesse de pico no período "
            f"(média de {avg:.0f}% ao longo de {points} pontos, últimas 12 meses)."
        )
        return SearchResult(
            source="google_trends",
            title=f"Google Trends: {query}",
            url=url,
            description=description,
            metrics={
                "peak_interest": peak,
                "avg_interest": avg,
                "last_interest": summary.get("last_interest", 0.0),
                "points": points,
            },
        )

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto em SearchResult."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        if isinstance(raw_result, dict):
            return SearchResult(
                source="google_trends",
                title=raw_result.get("title", "Google Trends"),
                url=raw_result.get("url", ""),
                description=raw_result.get("description", ""),
            )
        return SearchResult(
            source="google_trends",
            title="Google Trends",
            url="",
            description=str(raw_result),
        )
