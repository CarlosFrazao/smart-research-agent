"""Planejador de fontes de busca por dominio e intencao do usuario.

Mapeia o domínio detectado pela análise de intenção para listas prioritizadas
de searchers primarios e secundários, e distribui as queries expandidas
entre os searchers mais compativel com cada tipo de busca.
"""

import logging
from pathlib import Path
from typing import Any

from src.types import ExpandedQuery, IntentResult, SourcePlan

logger = logging.getLogger(__name__)

DOMAIN_SOURCES: dict[str, dict[str, list[str]]] = {
    "saas_b2b": {
        "primary": ["github", "producthunt", "reddit", "searxng"],
        "secondary": ["hackernews", "awesome", "firecrawl", "stackoverflow"],
    },
    "dev_tools": {
        "primary": ["github", "hackernews", "reddit", "stackoverflow"],
        "secondary": ["awesome", "arxiv", "firecrawl", "searxng", "wayback"],
    },
    "ai_ml": {
        "primary": ["arxiv", "github", "hackernews", "rss", "searxng"],
        "secondary": ["reddit", "firecrawl", "producthunt", "stackoverflow"],
    },
    "automation": {
        "primary": ["github", "reddit", "producthunt", "stackoverflow"],
        "secondary": ["hackernews", "awesome", "firecrawl", "rss", "searxng"],
    },
    "infrastructure": {
        "primary": ["github", "hackernews", "awesome", "stackoverflow"],
        "secondary": ["reddit", "arxiv", "firecrawl", "searxng", "wayback"],
    },
    "open_source": {
        "primary": ["github", "awesome", "hackernews", "searxng"],
        "secondary": ["reddit", "producthunt", "firecrawl", "stackoverflow"],
    },
    "general": {
        "primary": ["github", "reddit", "hackernews", "firecrawl", "searxng"],
        "secondary": ["producthunt", "arxiv", "awesome", "stackoverflow", "wayback"],
    },
}


class SourcePlanner:
    """Planeja a distribuição de buscas entre searchers com base no dominio.

    Usa um mapa de domínio-fontes (via `domains.yaml` ou embutido) para
    determinar quais searchers são primários e secundários para cada
    tipo de pesquisa detectado pelo `IntentAnalyzer`.
    """

    def __init__(self, config: dict[str, Any] = None):
        self.config = config or {}
        self.domain_map = self._load_domain_map()

    def _load_domain_map(self) -> dict:
        """Carrega o mapeamento de dominios para fontes do arquivo YAML de config.

        Tenta ler de ``config/domains.yaml`` relativo ao projeto. Em caso de
        falha (arquivo ausente ou YAML inválido), usa `DOMAIN_SOURCES` embutido.

        Returns:
            dict: Mapa de dominio -> {primary: [...], secondary: [...]}.
        """
        config_path = Path(__file__).parent.parent / "config" / "domains.yaml"
        if config_path.exists():
            try:
                import yaml

                with open(config_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    return data.get("domains", DOMAIN_SOURCES)
            except Exception as e:
                logger.warning(f"Erro ao carregar domains.yaml: {e}")
        return DOMAIN_SOURCES

    def plan(self, intent: IntentResult, queries: list[ExpandedQuery]) -> SourcePlan:
        """Gera um plano de buscas priorizando as fontes mais relevantes para o dominio.

        Args:
            intent: Resultado da analise de intencao contendo o dominio detectado.
            queries: Lista de queries expandidas geradas pelo `QueryExpander`.

        Returns:
            SourcePlan: Plano com sources mapeando cada searcher as suas queries,
                mais listas separadas de searchers primarios e secundarios.
        """
        domain_key = intent.domain.value
        mapping = self.domain_map.get(domain_key, DOMAIN_SOURCES["general"])

        primary = mapping.get("primary", [])
        secondary = mapping.get("secondary", [])

        plan: dict[str, list[ExpandedQuery]] = {}
        for source in primary + secondary:
            plan[source] = self._select_queries_for_source(queries, source, intent)

        return SourcePlan(sources=plan, primary=primary, secondary=secondary)

    def _select_queries_for_source(
        self, queries: list[ExpandedQuery], source: str, intent: IntentResult
    ) -> list[ExpandedQuery]:
        """Assign queries to each source based on type compatibility.

        The QueryExpander generates types: synonym, perspective, evidence,
        community, academic, temporal, original, qualificador, comparacao,
        plataforma, caso_de_uso.
        This method maps all of them to the right sources.
        """
        # Type sets that each source prefers
        GITHUB_TYPES = {
            "plataforma",
            "qualificador",
            "synonym",
            "evidence",
            "temporal",
            "original",
        }
        REDDIT_TYPES = {"caso_de_uso", "comparacao", "community", "perspective"}
        HN_TYPES = {"plataforma", "comparacao", "community", "perspective", "evidence"}
        ARXIV_TYPES = {"academic", "evidence", "synonym"}
        PH_TYPES = {"qualificador", "comparacao", "community", "temporal"}
        AWESOME_TYPES = {"qualificador", "plataforma", "synonym", "evidence"}
        STACKOVERFLOW_TYPES = {"community", "evidence", "perspective", "caso_de_uso"}
        WAYBACK_TYPES = {"temporal", "evidence", "original"}
        WEB_TYPES = set()  # accepts everything
        FIRECRAWL_TYPES = set()  # accepts everything
        SEARXNG_TYPES = set()  # accepts everything

        source_type_map = {
            "github": GITHUB_TYPES,
            "reddit": REDDIT_TYPES,
            "hackernews": HN_TYPES,
            "arxiv": ARXIV_TYPES,
            "producthunt": PH_TYPES,
            "awesome": AWESOME_TYPES,
            "stackoverflow": STACKOVERFLOW_TYPES,
            "wayback": WAYBACK_TYPES,
            "web": WEB_TYPES,
            "firecrawl": FIRECRAWL_TYPES,
            "searxng": SEARXNG_TYPES,
            "rss": set(),  # accepts everything — scored by keyword overlap
        }

        accepted_types = source_type_map.get(source, set())
        selected = []

        for q in queries:
            # Empty accepted_types = accept all (web, firecrawl)
            if not accepted_types or q.type in accepted_types:
                selected.append(q)

        # If no type match found, fall back to all high-priority queries
        if len(selected) < 2:
            high_priority = [q for q in queries if q.priority == "alta"]
            for q in high_priority:
                if q not in selected:
                    selected.append(q)

        # Last resort: take first 3 queries regardless of type
        if len(selected) == 0:
            selected = list(queries[:3])

        # Deduplicate
        seen: set = set()
        unique = []
        for q in selected:
            if q.query not in seen:
                seen.add(q.query)
                unique.append(q)

        return unique[:5]
