from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

class WaybackSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_url = "https://archive.org/wayback/available"

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        # Para o Wayback, a query ideal é uma URL. Se não for uma URL válida,
        # retornamos fallback já que o Wayback busca snapshots de URLs específicas.
        if not (query.startswith("http://") or query.startswith("https://")):
            logger.info("WaybackSearcher: A query fornecida não é uma URL. Ignorando busca no Wayback Machine.")
            return []
            
        params = {
            "url": query
        }
        
        logger.info(f"WaybackSearcher: Consultando histórico de capturas para '{query}'...")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.api_url, params=params)
                
                if response.status_code != 200:
                    logger.warning(f"WaybackSearcher: Erro ao consultar API (HTTP {response.status_code})")
                    return self.fallback(query)
                    
                data = response.json()
                archived = data.get("archived_snapshots", {})
                
                if not archived or "closest" not in archived:
                    logger.info(f"WaybackSearcher: Nenhuma captura arquivada encontrada para {query}")
                    return []
                    
                closest = archived["closest"]
                logger.info(f"WaybackSearcher: Captura mais próxima encontrada em {closest.get('timestamp')}")
                
                return [self.normalize(closest)]
                
        except Exception as e:
            logger.error(f"WaybackSearcher: Falha ao executar busca no Internet Archive: {e}")
            return self.fallback(query)

    def normalize(self, raw_result: Dict[str, Any]) -> SearchResult:
        timestamp_str = raw_result.get("timestamp", "")
        # Formata o timestamp de AAAAMMDDHHMMSS para data legível
        readable_date = "Data desconhecida"
        if timestamp_str and len(timestamp_str) >= 8:
            try:
                dt = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
                readable_date = dt.strftime("%d/%m/%Y %H:%M:%S UTC")
            except Exception:
                pass
                
        url = raw_result.get("url", "")
        
        description = (
            f"Captura histórica arquivada no Internet Archive Wayback Machine. "
            f"Data do Snapshot: {readable_date}. Status original: {raw_result.get('status')}."
        )
        
        return SearchResult(
            source="wayback",
            title=f"Histórico: Snapshot de {readable_date}",
            url=url,
            description=description,
            metrics={
                "source_domain": "archive.org",
                "timestamp": timestamp_str,
                "status": raw_result.get("status", "unknown")
            },
            raw=raw_result
        )
