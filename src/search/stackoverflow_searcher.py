from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
import logging
import httpx
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []
        
    def handle_data(self, d):
        self.text.append(d)
        
    def get_data(self):
        return ''.join(self.text)


class StackOverflowSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_url = "https://api.stackexchange.com/2.3/search/advanced"
        self.site = config.get("stackoverflow_site", "stackoverflow")

    def _strip_html(self, html: str) -> str:
        try:
            s = MLStripper()
            s.feed(html)
            return s.get_data()
        except Exception:
            return html

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        params = {
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": self.site,
            "pagesize": self.max_results
        }
        
        logger.info(f"StackOverflowSearcher: Consultando '{query[:50]}' no StackOverflow...")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.api_url, params=params)
                
                if response.status_code != 200:
                    logger.warning(f"StackOverflowSearcher: Erro ao consultar API (HTTP {response.status_code})")
                    return self.fallback(query)
                    
                data = response.json()
                items = data.get("items", [])
                logger.info(f"StackOverflowSearcher: {len(items)} perguntas encontradas.")
                
                return [self.normalize(item) for item in items]
                
        except Exception as e:
            logger.error(f"StackOverflowSearcher: Falha ao executar busca: {e}")
            return self.fallback(query)

    def normalize(self, raw_result: Dict[str, Any]) -> SearchResult:
        # Tenta remover HTML do título se houver entities
        title = self._strip_html(raw_result.get("title", "Sem título"))
        url = raw_result.get("link", "")
        
        # Tags ajudam no contexto das pesquisas
        tags = raw_result.get("tags", [])
        
        # Cria uma descrição sumarizada baseada no score e tags
        description = (
            f"Perguntado por: {raw_result.get('owner', {}).get('display_name', 'Desconhecido')}. "
            f"Tags: {', '.join(tags)}. Score: {raw_result.get('score', 0)}. "
            f"Visualizações: {raw_result.get('view_count', 0)}. Respondida: {'Sim' if raw_result.get('is_answered') else 'Não'}."
        )
        
        return SearchResult(
            source="stackoverflow",
            title=title,
            url=url,
            description=description,
            metrics={
                "source_domain": "stackoverflow.com",
                "score": raw_result.get("score", 0),
                "tags": tags,
                "answer_count": raw_result.get("answer_count", 0),
                "is_answered": raw_result.get("is_answered", False)
            },
            raw=raw_result
        )
