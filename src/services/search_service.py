import asyncio
import logging
from urllib.parse import quote
from datetime import datetime
from typing import List, Any, Dict, Optional

from src.types import SearchResult, ExpandedQuery
from src.query_validator import QueryValidator

logger = logging.getLogger("orchestrator.search_service")


class SearchService:
    """
    Gerencia a execução paralela de buscas e o cascateamento inteligente de scrapers.
    """

    def __init__(self, orchestrator):
        self.orch = orchestrator

    @property
    def config(self):
        return self.orch.config

    @property
    def searchers(self):
        return self.orch.searchers

    @property
    def cache(self):
        return self.orch.cache

    @property
    def health_monitor(self):
        return self.orch.health_monitor

    @property
    def operation_mode(self):
        return self.orch.operation_mode

    async def select_scraper_for_url(self, url: str) -> List[SearchResult]:
        """
        Smart cascade: tenta scrapers em ordem de prioridade para a URL dada.
        """
        firecrawl = self.searchers.get("firecrawl")
        spider = self.searchers.get("spider")
        steel = self.searchers.get("steel")

        # 1. Tentativa primária: Firecrawl
        if firecrawl:
            try:
                result = await asyncio.wait_for(firecrawl.search(url), timeout=10.0)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
                logger.warning(f"Firecrawl content too short/empty for '{url[:50]}'")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Firecrawl failed for '{url[:50]}': {e}")

        # 2. Tentativa secundária: Spider (se habilitado)
        if spider and self.config.spider_enabled:
            try:
                result = await spider.search(url)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
            except Exception as e:
                logger.warning(f"Spider failed for '{url[:50]}': {e}")

        # 3. Tentativa terciária: Steel (se habilitado)
        if steel and self.config.steel_enabled:
            try:
                result = await steel.search(url)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
            except Exception as e:
                logger.warning(f"Steel failed for '{url[:50]}': {e}")

        # 4. Playwright Stealth (se habilitado) — browser real para sites JS-heavy / WAF
        playwright = self.searchers.get("playwright")
        if playwright and getattr(self.config, "playwright_enabled", False):
            try:
                logger.info(f"Tentando Playwright Stealth para '{url[:50]}'")
                result = await asyncio.wait_for(playwright.search(url), timeout=35.0)
                if result and result[0].description and len(result[0].description.strip()) > 200:
                    return result
                logger.warning(f"Playwright retornou conteúdo curto/vazio para '{url[:50]}'")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Playwright falhou para '{url[:50]}': {e}")

        # 5. Fallback incondicional final: Jina Reader
        jina_url = f"https://r.jina.ai/{quote(url, safe=':/.?=#&')}"
        try:
            logger.info(f"Using Jina Reader fallback for '{url[:50]}'")
            raw = await self.searchers["firecrawl"].client.scrape(jina_url)
            if raw and raw.get("markdown"):
                return [SearchResult(
                    source="jina_reader",
                    title=f"Jina: {url}",
                    url=url,
                    description=str(raw.get("markdown", "")),
                    metrics={},
                    raw=raw,
                )]
        except Exception as e:
            logger.warning(f"Jina Reader failed for '{url[:50]}': {e}")

        # Fallback de desespero
        if firecrawl:
            try:
                result = await firecrawl.search(url)
                if result:
                    return result
            except Exception:
                pass

        return []

    async def execute(self, queries: List[ExpandedQuery], plan, intent) -> List[SearchResult]:
        """
        Executa as pesquisas planejadas em paralelo, respeitando cache e modo de operação.
        """
        tasks = []
        results = []

        # Injeta RSSSearcher se for query urgente/recente de tecnologia e o RSS estiver habilitado
        if intent.urgency == "sim" and intent.domain.value in ("ai_ml", "dev_tools", "saas_b2b") and queries:
            rss = self.searchers.get("rss")
            if rss and rss.enabled:
                primary_query = queries[0].query
                cache_key = f"rss:{primary_query}"
                cached = self.cache.get("search", cache_key)
                if cached is not None:
                    logger.debug(f"Cache hit para RSS: {cache_key}")
                    deserialized = []
                    for r in cached:
                        if "fetched_at" in r and isinstance(r["fetched_at"], str):
                            try:
                                r["fetched_at"] = datetime.fromisoformat(r["fetched_at"])
                            except Exception:
                                r["fetched_at"] = datetime.now()
                        deserialized.append(SearchResult(**r))
                    results.extend(deserialized)
                else:
                    task = asyncio.create_task(
                        self._search_task(rss, "rss", primary_query, intent.domain.value),
                        name=f"rss:{primary_query[:30]}",
                    )
                    tasks.append(task)

        for source_name, source_queries in plan.sources.items():
            if source_name not in self.operation_mode.searchers:
                logger.debug(f"Searcher '{source_name}' filtrado (desabilitado no modo '{self.operation_mode.name}')")
                continue
            searcher = self.searchers.get(source_name)
            if not searcher or not searcher.enabled:
                continue
            for eq in source_queries:
                sanitized = QueryValidator.sanitize(eq.query)
                if not QueryValidator.is_valid(sanitized):
                    logger.warning(f"Query desconsiderada por ser inválida ou malformada: '{eq.query[:50]}'")
                    continue
                eq.query = sanitized
                cache_key = f"{source_name}:{eq.query}"
                cached = self.cache.get("search", cache_key)
                if cached is not None:
                    logger.debug(f"Cache hit: {cache_key}")
                    deserialized = []
                    for r in cached:
                        if "fetched_at" in r and isinstance(r["fetched_at"], str):
                            try:
                                r["fetched_at"] = datetime.fromisoformat(r["fetched_at"])
                            except Exception:
                                r["fetched_at"] = datetime.now()
                        deserialized.append(SearchResult(**r))
                    results.extend(deserialized)
                    continue

                task = asyncio.create_task(
                    self._search_task(searcher, source_name, eq.query, intent.domain.value),
                    name=f"{source_name}:{eq.query[:30]}",
                )
                tasks.append(task)

        for task in asyncio.as_completed(tasks):
            try:
                source_name, query_str, res = await task
                results.extend(res)
                if res:
                    self.cache.set(
                        "search",
                        f"{source_name}:{query_str}",
                        [r.__dict__ for r in res],
                    )
            except Exception as e:
                logger.warning(f"Busca falhou: {e}")

        return results

    async def _search_task(self, searcher, source_name: str, query: str, domain: str):
        from src.utils.logger import structured_logger
        error_msg = None
        res = []
        try:
            res = await self._search_with_timeout(searcher, query, domain)
        except Exception as e:
            error_msg = str(e)
        structured_logger.log_search(source_name, query, len(res), error_msg)
        return source_name, query, res

    async def _search_with_timeout(self, searcher, query: str, domain: str):
        try:
            return await asyncio.wait_for(
                searcher.search(query, domain=domain),
                timeout=searcher.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout em {searcher.__class__.__name__}")
            if self.health_monitor:
                cls_name = searcher.__class__.__name__.lower()
                source_name = "hackernews" if "hn" in cls_name else cls_name.replace("searcher", "")
                self.health_monitor.report_failure(source_name, "TimeoutError")
            return searcher.fallback(query)
        except Exception as e:
            logger.error(f"Erro em {searcher.__class__.__name__}: {e}")
            if self.health_monitor:
                cls_name = searcher.__class__.__name__.lower()
                source_name = "hackernews" if "hn" in cls_name else cls_name.replace("searcher", "")
                self.health_monitor.report_failure(source_name, str(e))
            return []
