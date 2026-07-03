from __future__ import annotations
import asyncio, logging
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
SearchResult = Dict[str, Any]


class MultilingualSearcher:
    '''
    Wrapper sobre qualquer searcher com search(query)->List[dict].
    Traduz para multiplos idiomas via LLM e deduplica por URL.
    Plano SRA v6.0 item 3.1
    '''

    _LANG_NAMES: Dict[str, str] = {
        'en': 'English', 'pt': 'Portuguese', 'es': 'Spanish',
        'fr': 'French', 'de': 'German', 'zh': 'Chinese',
        'ja': 'Japanese', 'ko': 'Korean', 'ru': 'Russian',
        'ar': 'Arabic', 'it': 'Italian',
    }

    def __init__(self, base_searcher: Any, llm_client: Optional[Any] = None, concurrency: int = 3) -> None:
        self.searcher = base_searcher
        self.llm = llm_client
        self.concurrency = concurrency

    async def _translate(self, text: str, target_lang: str) -> str:
        if self.llm is None:
            return text
        lang_name = self._LANG_NAMES.get(target_lang, target_lang)
        prompt = (
            f'Translate the following search query to {lang_name}. '
            f'Return ONLY the translated query.\n\nQuery: {text}'
        )
        try:
            result: str = await self.llm.complete(prompt)
            return result.strip() or text
        except Exception as exc:
            logger.warning(f'Traducao para {target_lang} falhou: {exc}')
            return text

    async def _search_one_lang(self, query: str, lang: str) -> List[SearchResult]:
        translated = await self._translate(query, lang)
        try:
            results = await self.searcher.search(translated)
            for r in results:
                r.setdefault('lang', lang)
            return results
        except Exception as exc:
            logger.warning(f'Busca em {lang} falhou: {exc}')
            return []

    async def search(
        self,
        query: str,
        languages: Optional[List[str]] = None,
        limit_per_lang: int = 10,
    ) -> List[SearchResult]:
        langs = languages or ['en']
        sem = asyncio.Semaphore(self.concurrency)

        async def _bounded(lang: str) -> List[SearchResult]:
            async with sem:
                return await self._search_one_lang(query, lang)

        batches = await asyncio.gather(*[_bounded(lg) for lg in langs])
        seen: set = set()
        merged: List[SearchResult] = []
        for batch in batches:
            for r in batch:
                url = r.get('url', '')
                if url and url not in seen:
                    seen.add(url)
                    merged.append(r)
        logger.info(f'MultilingualSearcher: {len(merged)} resultados unicos de {len(langs)} idioma(s).')
        return merged