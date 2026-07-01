"""
youtube_searcher.py — Buscador de vídeos e conteúdo audiovisual do YouTube via Data API v3

Processo de Busca:
1. Search: Obtém a lista de IDs de vídeo correspondentes à query.
2. Videos Statistics: Obtém estatísticas de visualizações e curtidas para pontuação de confiança.
Rate-limits: Cota diária padrão do YouTube v3 (10.000 unidades).
Fallback: Conecta ao WebSearcher se retornar < 2 resultados ou se a API key estiver ausente.
"""
import logging
import asyncio
from typing import List, Dict, Any, Optional

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeSearcher(BaseSearcher):
    """
    Buscador de conteúdo científico/técnico no YouTube.
    Extrai estatísticas de visualizações e curtidas para cálculo de métricas de relevância.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.http = HTTPClient(timeout=self.timeout)
        self.api_key: Optional[str] = config.get("youtube_api_key")
        self.web_fallback = None  # Injetado pelo Orchestrator se disponível

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.api_key:
            logger.info("YouTubeSearcher: API Key não configurada. Acionando web fallback diretamente.")
            return await self._run_web_fallback(query)

        # 1. Busca vídeos
        search_params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "key": self.api_key,
            "maxResults": min(self.max_results, 15),
        }

        try:
            search_resp = await self.http.get(_SEARCH_URL, params=search_params)
            search_data = search_resp.get("json", {}) or {}
            items = search_data.get("items", [])

            if not items:
                logger.info(f"YouTubeSearcher: nenhum vídeo encontrado para a query '{query[:40]}'")
                return await self._run_web_fallback(query)

            # Mapeia vídeo ID para informações do snippet
            video_map = {}
            for item in items:
                video_id = item.get("id", {}).get("videoId")
                if video_id:
                    video_map[video_id] = item.get("snippet", {})

            # 2. Busca estatísticas (views e likes)
            video_ids = list(video_map.keys())
            stats_params = {
                "part": "statistics",
                "id": ",".join(video_ids),
                "key": self.api_key,
            }

            stats_resp = await self.http.get(_VIDEOS_URL, params=stats_params)
            stats_data = stats_resp.get("json", {}) or {}
            stats_items = stats_data.get("items", [])

            stats_map = {}
            for s_item in stats_items:
                v_id = s_item.get("id")
                if v_id:
                    stats_map[v_id] = s_item.get("statistics", {})

            # 3. Processa e calcula confiança
            results = []
            for v_id, snippet in video_map.items():
                stats = stats_map.get(v_id, {})
                parsed = self._parse_video(v_id, snippet, stats)
                if parsed:
                    results.append(parsed)

            logger.info(f"YouTubeSearcher: {len(results)} vídeos processados para '{query[:40]}'")

            if len(results) < 2:
                logger.info("YouTubeSearcher: resultados insuficientes. Acionando fallback...")
                fallback_res = await self._run_web_fallback(query)
                results.extend(fallback_res)

            return results[:self.max_results]

        except Exception as e:
            logger.error(f"YouTube search error: {e}")
            return self.fallback(query)

    def _parse_video(self, video_id: str, snippet: Dict[str, Any], stats: Dict[str, Any]) -> Optional[SearchResult]:
        """
        Converte as informações brutas do YouTube v3 em SearchResult.
        """
        try:
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            channel = snippet.get("channelTitle", "")
            pub_date = snippet.get("publishedAt", "")
            url = f"https://www.youtube.com/watch?v={video_id}"

            views_raw = stats.get("viewCount", "0")
            likes_raw = stats.get("likeCount", "0")

            try:
                views = int(views_raw)
            except ValueError:
                views = 0

            try:
                likes = int(likes_raw)
            except ValueError:
                likes = 0

            # Pontuação de confiança simples baseada em engajamento
            ratio = likes / views if views > 50 else 0.0
            engagement_score = min(0.3, ratio * 3.0)  # máx 0.3
            popularity_score = min(0.5, (views / 500000.0))  # máx 0.5 para 500k views
            confidence = round(0.4 + engagement_score + popularity_score, 2)
            confidence = min(1.0, max(0.1, confidence))

            desc_parts = [
                description[:300],
                f"Canal: {channel}.",
                f"Visualizações: {views:,}.",
                f"Likes: {likes:,}.",
                f"Publicado em: {pub_date[:10]}."
            ]

            res = SearchResult(
                source="youtube",
                title=title,
                url=url,
                description=" ".join(desc_parts),
                metrics={
                    "views": views,
                    "likes": likes,
                    "channel": channel,
                    "published_at": pub_date
                }
            )
            res.confidence_score = confidence
            return res
        except Exception as e:
            logger.warning(f"YouTubeSearcher: erro ao parsear vídeo {video_id}: {e}")
            return None

    async def _run_web_fallback(self, query: str) -> List[SearchResult]:
        """
        Executa busca na web como fallback se o YouTube falhar ou retornar vazio.
        """
        if self.web_fallback and getattr(self.web_fallback, "enabled", False):
            try:
                logger.info(f"YouTube: executando web fallback para '{query[:40]}'")
                return await self.web_fallback.search(f"YouTube video {query}")
            except Exception as e:
                logger.warning(f"YouTube: falha no web fallback: {e}")
        return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """
        Normaliza um resultado bruto para o formato SearchResult.
        """
        return SearchResult(
            source="youtube",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics=raw_result.get("metrics", {}),
            raw=raw_result,
        )
