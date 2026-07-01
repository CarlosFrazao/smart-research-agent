"""
pubmed_searcher.py — Buscador de artigos biomédicos e médicos via NCBI PubMed API

Processo de Busca:
1. ESearch: Obtém a lista de IDs dos artigos relevantes.
2. ESummary: Obtém os detalhes resumidos correspondentes aos IDs.
Rate-limits: NCBI limita a 3 requisições/s sem API key.
Fallback: Conecta ao WebSearcher se retornar < 2 resultados.
"""
import re
import logging
import asyncio
from typing import List, Dict, Any, Optional

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


class PubMedSearcher(BaseSearcher):
    """
    Buscador de literatura médica e científica utilizando a API NCBI Entrez.
    Realiza busca em duas etapas (esearch -> esummary) e suporta fallback.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.http = HTTPClient(timeout=self.timeout)
        self.api_key: Optional[str] = config.get("ncbi_api_key")
        self.web_fallback = None  # Injetado pelo Orchestrator se disponível

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        headers = {}
        
        # 1. ESearch — Buscar IDs
        search_params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": min(self.max_results, 20),
        }
        if self.api_key:
            search_params["api_key"] = self.api_key

        try:
            # Respeita o rate-limit geral da NCBI (máx 3 req/s sem key)
            await asyncio.sleep(0.35)
            
            search_resp = await self.http.get(_ESEARCH_URL, params=search_params, headers=headers)
            search_data = search_resp.get("json", {}) or {}
            id_list = search_data.get("esearchresult", {}).get("idlist", [])

            if not id_list:
                logger.info(f"PubMedSearcher: nenhum ID encontrado para a query '{query[:40]}'")
                return await self._run_web_fallback(query)

            # 2. ESummary — Buscar detalhes dos IDs
            summary_params = {
                "db": "pubmed",
                "id": ",".join(id_list),
                "retmode": "json",
            }
            if self.api_key:
                summary_params["api_key"] = self.api_key

            await asyncio.sleep(0.35)
            summary_resp = await self.http.get(_ESUMMARY_URL, params=summary_params, headers=headers)
            summary_data = summary_resp.get("json", {}) or {}
            uid_results = summary_data.get("result", {})

            results = []
            # O result do esummary vem indexado por ID em formato string, além de um campo 'uids'
            for uid in id_list:
                paper_info = uid_results.get(str(uid))
                if not paper_info:
                    continue
                
                parsed = self._parse_summary(uid, paper_info)
                if parsed:
                    results.append(parsed)

            logger.info(f"PubMedSearcher: {len(results)} artigos encontrados para '{query[:40]}'")

            if len(results) < 2:
                logger.info("PubMedSearcher: resultados insuficientes. Acionando fallback...")
                fallback_res = await self._run_web_fallback(query)
                results.extend(fallback_res)

            return results[:self.max_results]

        except Exception as e:
            logger.error(f"PubMed search error: {e}")
            return self.fallback(query)

    def _parse_summary(self, uid: str, info: Dict[str, Any]) -> Optional[SearchResult]:
        """
        Converte as informações brutas do ESummary da NCBI em SearchResult.
        """
        try:
            title = info.get("title", "")
            # Limpa tags HTML/XML residuais do título do PubMed se houver (ex: <i>, <b>)
            title = re.sub(r"<[^>]+>", "", title)

            pub_date = info.get("pubdate", "")
            source = info.get("source", "")
            
            authors_raw = info.get("authors", [])
            authors_names = [a.get("name", "") for a in authors_raw[:3] if a]
            authors_str = ", ".join(authors_names)
            if len(authors_raw) > 3:
                authors_str += " et al."

            url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"

            # Monta descrição
            desc_parts = []
            if source:
                desc_parts.append(f"Publicado em: {source}.")
            if pub_date:
                desc_parts.append(f"Data: {pub_date}.")
            if authors_str:
                desc_parts.append(f"Autores: {authors_str}.")
            
            # Detalhes adicionais se existirem
            article_ids = info.get("articleids", [])
            doi = ""
            for aid in article_ids:
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break
            
            if doi:
                desc_parts.append(f"DOI: {doi}.")

            return SearchResult(
                source="pubmed",
                title=title,
                url=url,
                description=" ".join(desc_parts),
                metrics={"pmid": uid, "pub_date": pub_date, "doi": doi},
            )
        except Exception as e:
            logger.warning(f"PubMedSearcher: erro ao parsear uid {uid}: {e}")
            return None

    async def _run_web_fallback(self, query: str) -> List[SearchResult]:
        """
        Executa busca na web como fallback se o PubMed falhar ou retornar vazio.
        """
        if self.web_fallback and getattr(self.web_fallback, "enabled", False):
            try:
                logger.info(f"PubMed: executando web fallback para '{query[:40]}'")
                return await self.web_fallback.search(f"PubMed article {query}")
            except Exception as e:
                logger.warning(f"PubMed: falha no web fallback: {e}")
        return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """
        Normaliza um resultado bruto para o formato SearchResult.
        """
        return SearchResult(
            source="pubmed",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics=raw_result.get("metrics", {}),
            raw=raw_result,
        )

