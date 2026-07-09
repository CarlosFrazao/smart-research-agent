"""DiscourseSearcher — Busca em fóruns Discourse via API JSON pública.

O Discourse expõe um endpoint de busca em JSON acessível sem autenticação:
  GET {base_url}/search.json?q={query}&page={page}

O formato da resposta inclui ``posts`` e ``topics``. Ambos são parseados para
``SearchResult``. Por padrão aponta para ``discuss.python.org`` (fórum oficial
do Python), mas a URL base é configurável via variável de ambiente
``DISCOURSE_BASE_URL`` ou via chave ``base_url`` no dict de configuração.

A fonte é marcada como não-confiável (``trusted=False``) pois retorna texto
livre de fórum — passa pelo ``LLMSanitizer`` em ``search_stage.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger("search.discourse")

DEFAULT_DISCOURSE_BASE_URL = "https://discuss.python.org"


@register_searcher("discourse", enabled_env="SRA_DISCOURSE_ENABLED", trusted=False)
class DiscourseSearcher(APISearcher):
    """Searcher para fóruns Discourse via API JSON pública.

    Busca posts e tópicos em fóruns Discourse e normaliza para SearchResult.
    Não requer API key, mas aceita ``DISCOURSE_BASE_URL`` para apontar a outro
    fórum (ex: ``https://meta.discourse.org``).

    Attributes:
        base_url: URL base do fórum Discourse (sem barra final).
    """

    def __init__(self, config: dict[str, Any]):
        # URL base configurável (não usamos pop para não mutar o dict compartilhado)
        base_url = config.get("base_url") or os.getenv(
            "DISCOURSE_BASE_URL", DEFAULT_DISCOURSE_BASE_URL
        )
        self._base_url = base_url.rstrip("/")

        api_config = APISearcherConfig(
            source_name="discourse",
            base_url=self._base_url,
            timeout=config.get("timeout", 30.0),
            max_results=config.get("max_results", 10),
            circuit_config=None,
            cache_ttl=config.get("cache_ttl", 3600),
        )
        super().__init__(api_config)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca no Discourse.

        Args:
            query: Termo de busca.
            **kwargs: Aceita ``page`` (int) para paginação.

        Returns:
            Lista de SearchResult com posts e tópicos encontrados.
        """
        page = int(kwargs.get("page", 1) or 1)
        params = {"q": query, "page": page}

        try:
            data = await self._make_request("GET", "/search.json", params=params)
            results: list[SearchResult] = []
            if isinstance(data, dict):
                results.extend(self._parse_posts(data.get("posts", [])))
                results.extend(self._parse_topics(data.get("topics", [])))
            logger.debug(
                f"DiscourseSearcher: {len(results)} resultados para '{query}' (page={page})"
            )
            return results[: self.max_results]
        except Exception as e:
            logger.warning(f"DiscourseSearcher falhou para '{query}': {e}")
            return []

    def _parse_posts(self, posts: Any) -> list[SearchResult]:
        """Converte a lista de posts da resposta em SearchResult."""
        results: list[SearchResult] = []
        if not isinstance(posts, list):
            return results
        for post in posts:
            if not isinstance(post, dict):
                continue
            topic_id = post.get("topic_id")
            topic_slug = post.get("topic_slug") or ""
            post_number = post.get("post_number") or 1
            topic_title = post.get("topic_title") or ""
            blurb = post.get("blurb") or ""
            if not topic_id:
                continue
            url = f"{self._base_url}/t/{topic_slug}/{topic_id}/{post_number}"
            results.append(
                SearchResult(
                    source="discourse",
                    title=topic_title or blurb[:80],
                    url=url,
                    description=blurb,
                    metrics={
                        "post_number": post_number,
                        "username": post.get("username", ""),
                        "topic_id": topic_id,
                    },
                )
            )
        return results

    def _parse_topics(self, topics: Any) -> list[SearchResult]:
        """Converte a lista de tópicos da resposta em SearchResult."""
        results: list[SearchResult] = []
        if not isinstance(topics, list):
            return results
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            topic_id = topic.get("id")
            slug = topic.get("slug") or ""
            title = topic.get("title") or ""
            blurb = topic.get("blurb") or ""
            if not topic_id:
                continue
            url = f"{self._base_url}/t/{slug}/{topic_id}"
            results.append(
                SearchResult(
                    source="discourse",
                    title=title,
                    url=url,
                    description=blurb,
                    metrics={
                        "posts_count": topic.get("posts_count", 0),
                        "likes_count": topic.get("like_count", 0),
                    },
                )
            )
        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um item bruto (post ou tópico) em SearchResult."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        if isinstance(raw_result, dict):
            if raw_result.get("topic_title") or raw_result.get("topic_id"):
                parsed = self._parse_posts([raw_result])
            else:
                parsed = self._parse_topics([raw_result])
            if parsed:
                return parsed[0]
        return SearchResult(
            source="discourse",
            title="Resultado desconhecido",
            url="",
            description=str(raw_result),
        )
