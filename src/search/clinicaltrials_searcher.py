"""
clinicaltrials_searcher.py — Buscador de ensaios clínicos via ClinicalTrials.gov API v2

Endpoint: https://clinicaltrials.gov/api/v2/studies?query.term={query}&pageSize={n}
Autenticação: nenhuma (pública). Requisitos de rate limit moderados.
Fallback: WebSearcher quando a API retorna < 2 resultados (padrão GAP1).

Formato de resposta (v2) — chaves principais:
    "studies": [ { "protocolSection": { ... }, "referencesModule": { ... }, "resultsModule": { ... }, ... } ]

No pipeline, usamos o "briefTitle" do "identificationModule" e a lista "conditions" do "conditionsModule" para descrição.
"""

from __future__ import annotations

import logging
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

_API_BASE = "https://clinicaltrials.gov/api/v2/studies"
# Query term no endpoint: duas palavras-chave Pt/EN comuns para doenças/areas
_QUERY_EKP = ["diabete", "diabetes", "câncer", "cancer", "alzheimer", "alzheimers"]


class ClinicalTrialsSearcher(BaseSearcher):
    """Buscador de ensaios clínicos via ClinicalTrials.gov API v2.

    Sem API key — endpoint público, mas gratuito para uso razoável. Usa Circuit Breaker
    para isolar falhas da rede e fallback para WebSearcher quando resultados nativos
    são insuficientes (padrão GAP1). Retorna um minimal subset consistente: nctId,
    briefTitle, overallStatus, phase, conditions, startDate, url.
    """

    def __init__(self, config: dict[str, Any]):
        """Inicializa com as configurações e injecta o HTTPClient.

        Args:
            config (dict[str, Any]): Configurações globais de pesquisa.
        """
        super().__init__(config)
        self.http = HTTPClient(timeout=self.timeout)
        self.web_fallback = (
            None  # injetado posteriormente pelo Orchestrator se disponível
        )
        self.circuit = CircuitBreakerRegistry.get(
            "clinicaltrials_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona de ensaios clínicos.

        Args:
            query (str): Consulta livre para busca.
            **kwargs: Parâmetros extra (não usados).

        Returns:
            list[SearchResult]: Resultados normalizados de ensaios, ou fallback Web.
        """
        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "clinicaltrials_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._do_search, query)
        except CircuitBreakerOpen as e:
            logger.warning(f"ClinicalTrialsSearcher: {e}")
            return self.fallback(query)
        except Exception as e:
            logger.error(f"ClinicalTrials search error: {e}")
            return self.fallback(query)

    async def _do_search(self, query: str) -> list[SearchResult]:
        """Chama a API v2, normaliza cada estudo, aplica fallback se produtos < 2."""
        # Tenta enriquecer o termo da query com sinônimos PT/EN para maior recall
        # usando uma lista simples de keywords (potencialmente expandível com LLM mais tarde)
        terms = self._expand_query(query)
        params = {
            "query.term": " OR ".join(terms),
            "pageSize": min(self.max_results, 50),
            "sort": "relevance",  # opcional — endpoint pode ignorar; mantido para uso futuro
        }

        response = await self.http.get(_API_BASE, params=params)
        # HTTPClient retorna JSON puro ({'studies': [...]}) ou wrapper legado {'json': ...}
        if isinstance(response, dict):
            data = response.get("json")
            if isinstance(data, dict):
                response = data  # usar wrapper legado se existir
        studies = response.get("studies", []) if isinstance(response, dict) else []

        # Normaliza cada estudo e ignora nulos (sem briefTitle)
        results: list[SearchResult] = []
        for study in studies:
            normalized = self._normalize_study(study)
            if normalized:
                results.append(normalized)

        logger.info(
            f"ClinicalTrialsSearcher: {len(results)} ensaios para '{query[:50]}'"
        )

        # GAP1: poucos resultados nativos → web fallback (mesmo padrão de PubMed/ClinicalTrials)
        if (
            len(results) < 2
            and self.web_fallback
            and getattr(self.web_fallback, "enabled", False)
        ):
            logger.info("ClinicalTrials: fallback para WebSearcher ativado.")
            try:
                web_results = await self.web_fallback.search(f"clinical trial {query}")
                results.extend(web_results[:5])
            except Exception as e:
                logger.warning(f"ClinicalTrials WebFallback falhou: {e}")

        return results[: self.max_results]

    def _expand_query(self, query: str) -> list[str]:
        """Expande a query nativa com sinônimos PT/EN comuns para aumentar recall.

        Rotina simples (FOCA) — pode ser substituída por um LLM com tempo/fluxo.
        """
        q = query.lower()
        # Se o usuário já incluiu um termo técnico conhecido, mantém a query original como fallback
        if any(kw in q for kw in _QUERY_EKP):
            return [q]
        # Caso contrário, adiciona alguns termos de sinônimos (PT->EN)
        synonyms: list[str] = [q]
        if "diabetes" in q:
            synonyms.append("diabetes mellitus")
        if "cancer" in q:
            synonyms.append("neoplasm")
        if "alzheimer" in q:
            synonyms.append("alzheimer disease")
        # Remove duplicatas preservando ordem
        seen: set[str] = set()
        expanded = []
        for term in synonyms:
            if term not in seen:
                seen.add(term)
                expanded.append(term)
        return expanded

    def _normalize_study(self, study: dict[str, Any]) -> SearchResult | None:
        """Extrai campos essenciais do "studies[i]" v2 para "SearchResult".

        Usado pelo pipeline em tempo real: priority->confidence, evitando uma chamada HTTP extra.
        Retorna None se faltar briefTitle ou nctId (estudo inútil para o SRA).
        """
        try:
            protocol = study.get("protocolSection", {})
            ident = protocol.get("identificationModule", {})
            cond_module = protocol.get("conditionsModule", {})
            status = protocol.get("statusModule", {})

            nct_id = ident.get("nctId", "")
            brief_title = ident.get("briefTitle", "")

            # Rejeita estudo sem identificador ou título
            if not nct_id or not brief_title:
                return None
            overall_status = status.get("overallStatus", "")
            phase = ""
            phases = protocol.get("designModule", {}).get("phases", [])
            if phases:
                phase = phases[0]
            start_date = ""
            start_struct = status.get("startDateStruct", {})
            if isinstance(start_struct, dict):
                start_date = start_struct.get("date", "")

            conditions = cond_module.get("conditions", [])
            description_parts: list[str] = []
            if brief_title:
                description_parts.append(f"Título: {brief_title}.")
            if conditions:
                description_parts.append(f"Condições: {', '.join(conditions)}.")
            if overall_status:
                description_parts.append(f"Status: {overall_status}.")
            if phase:
                description_parts.append(f"Fase: {phase}.")
            if start_date:
                description_parts.append(f"Início: {start_date}.")

            # URL de referência: https://clinicaltrials.gov/study/{nct_id}
            url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""

            return SearchResult(
                source="clinicaltrials",
                title=brief_title,
                url=url,
                description=" ".join(description_parts),
                published_at=None,  # sem timestamp ISO puro; usa fetched_at para frescor
                metrics={
                    "nct_id": nct_id,
                    "overall_status": overall_status,
                    "phase": phase,
                    "start_date": start_date,
                    "conditions": conditions,
                },
                raw=study,
            )
        except Exception as e:
            logger.warning(
                f"ClinicalTrialsSearcher: falha ao normalizar estudo {study.get('nctId', '?')}: {e}"
            )
            return None

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto para o formato base.

        Chamado pelo infrastructure genérica do pipeline (ex: StageFactory).
        """
        if isinstance(raw_result, SearchResult):
            return raw_result
        # Se já for um SearchResult, retorna; caso contrário, chama _normalize_study.
        if isinstance(raw_result, dict):
            normalized = self._normalize_study(raw_result)
            if normalized is not None:
                return normalized
        return SearchResult(
            source="clinicaltrials",
            title=raw_result.get("title", "") if isinstance(raw_result, dict) else "",
            url=raw_result.get("url", "") if isinstance(raw_result, dict) else "",
            description=raw_result.get("description", "")
            if isinstance(raw_result, dict)
            else "",
            raw=raw_result if isinstance(raw_result, dict) else {},
        )
