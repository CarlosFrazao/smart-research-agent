"""Planejador de fontes de busca por dominio e intencao do usuario.

Mapeia o domínio detectado pela análise de intenção para listas prioritizadas
de searchers primarios e secundários, e distribui as queries expandidas
entre os searchers mais compativel com cada tipo de busca.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.types import ExpandedQuery, IntentResult, SourcePlan

logger = logging.getLogger(__name__)

# Prompt embutido de fallback para o Universal Router (Fase 2)
UNIVERSAL_PLANNER_PROMPT = """Você é um planejador de fontes de pesquisa.

Query: {query}
Domínio detectado: {domain}
Intenção: {intent}
Fontes disponíveis: {available_sources}

Liste as 3-6 fontes mais relevantes para responder esta query.
Responda APENAS com os nomes das fontes separados por vírgula, ex: wikipedia, duckduckgo, reddit
Sem explicação, sem markdown."""

DOMAIN_SOURCES: dict[str, dict[str, list[str]]] = {
    "saas_b2b": {
        "primary": ["github", "producthunt", "notion", "confluence", "searxng"],
        "secondary": [
            "hackernews",
            "sharepoint",
            "awesome",
            "firecrawl",
            "stackoverflow",
        ],
    },
    "dev_tools": {
        "primary": ["github", "confluence", "notion", "stackoverflow"],
        "secondary": [
            "awesome",
            "arxiv",
            "sharepoint",
            "firecrawl",
            "searxng",
            "wayback",
        ],
    },
    "ai_ml": {
        "primary": ["arxiv", "github", "notion", "rss", "searxng"],
        "secondary": [
            "reddit",
            "confluence",
            "sharepoint",
            "firecrawl",
            "producthunt",
            "open_library",
            "core_ac_uk",
            "doaj",
            "openalex",
        ],
    },
    "automation": {
        "primary": ["github", "confluence", "notion", "sharepoint"],
        "secondary": ["hackernews", "awesome", "firecrawl", "rss", "searxng"],
    },
    "infrastructure": {
        "primary": ["github", "confluence", "sharepoint", "awesome"],
        "secondary": [
            "reddit",
            "arxiv",
            "firecrawl",
            "searxng",
            "wayback",
            "osm_nominatim",
        ],
    },
    "open_source": {
        "primary": ["github", "awesome", "hackernews", "searxng"],
        "secondary": ["notion", "confluence", "sharepoint", "reddit", "producthunt"],
    },
    "general": {
        "primary": ["github", "notion", "confluence", "searxng"],
        "secondary": [
            "sharepoint",
            "producthunt",
            "arxiv",
            "awesome",
            "stackoverflow",
            "wayback",
            "open_library",
        ],
    },
    "news": {
        "primary": ["gdelt", "google_news_rss", "newsapi_org"],
        "secondary": ["bluesky", "mastodon_social", "reddit", "hackernews"],
    },
}

# Palavras que sinalizam intenção de notícia/evento atual (Fase 5 — Parte 4).
# Espelho defensivo das keywords do IntentAnalyzer: usado como fallback de
# roteamento caso o domínio chegue como GENERAL mas a query seja noticiosa.
NEWS_KEYWORDS = [
    "notícia",
    "noticias",
    "aconteceu",
    "hoje",
    "semana",
    "eleição",
    "eleicao",
    "governo",
    "economia",
    "esporte",
    "guerra",
    "política",
    "politica",
    "atualidade",
    "mundo",
    "breaking",
    "news",
    "today",
    "happening",
]
TECH_KEYWORDS = [
    "python",
    "api",
    "github",
    "npm",
    "docker",
    "framework",
    "library",
    "package",
    "bug",
    "code",
    "programming",
    "rust",
    "kubernetes",
    "terraform",
    "aws",
    "llm",
    "model",
]


class SourcePlanner:
    """Planeja a distribuição de buscas entre searchers com base no dominio.

    Usa um mapa de domínio-fontes (via `domains.yaml` ou embutido) para
    determinar quais searchers são primários e secundários para cada
    tipo de pesquisa detectado pelo `IntentAnalyzer`.
    """

    def __init__(
        self,
        config: dict[str, Any] = None,
        llm: Any = None,
        feedback_store: Any = None,
        user_id: str | None = None,
        mode: str | None = None,
    ):
        self.config = config or {}
        self.llm = llm
        self.feedback_store = feedback_store
        self.user_id = user_id
        self.mode = mode
        self.domain_map = self._load_domain_map()

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Executa uma coroutine a partir de contexto sincrono de forma segura.

        O ``plan()`` e o ``_plan_universal_with_llm()`` sao sincronos (chamados
        por estagios do pipeline), mas o roteamento LLM e assincrono. Este
        helper permite aguardar a coroutine sem quebrar se ja houver um loop de
        eventos ativo (roda a coroutine em uma thread dedicada com seu proprio
        loop, evitando deadlock).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return asyncio.run(coro)
        # Ja estamos dentro de um loop ativo: roda em thread separada.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(coro))
            return future.result()

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

    def _apply_user_weights(self, sources: list[str], domain: str) -> list[str]:
        """Reordena fontes conforme o peso histórico do usuário.

        Fontes com maior peso (historico de utilidade) ficam no inicio da lista,
        recebendo maior prioridade no plano de busca. Se nao houver feedback_store
        ou user_id configurado, a ordem original é preservada (sem personalizacao).

        Args:
            sources: Lista de nomes de searchers a reordenar.
            domain: Dominio/categoria da query para consultar os pesos.

        Returns:
            list[str]: Fontes ordenadas por peso decrescente (mais uteis primeiro).
        """
        if not self.feedback_store or not self.user_id:
            return sources  # sem personalização → ordem padrão

        try:
            weights = self.feedback_store.get_source_weights(
                self.user_id, domain, sources
            )
        except Exception as e:
            logger.warning(f"Falha ao obter pesos de fonte (ignorado): {e}")
            return sources

        return sorted(sources, key=lambda s: weights.get(s, 1.0), reverse=True)

    def plan(
        self, intent: IntentResult, queries: list[ExpandedQuery], context: dict = None
    ) -> SourcePlan:
        """Gera um plano de buscas priorizando as fontes mais relevantes para o dominio.

        Aplica roteamento estático para dominios tecnicos conhecidos e
        roteamento dinamico via LLM para o dominio ``universal`` (ou quando o
        dominio detectado nao consta no mapa). O LLM é sempre complementar à
        tabela estatica: a lista resultante é mesclada com as fontes do yaml.

        Args:
            intent: Resultado da analise de intencao contendo o dominio detectado.
            queries: Lista de queries expandidas geradas pelo `QueryExpander`.
            context: Contexto opcional contendo `extra` (com `trust_rules`)

        Returns:
            SourcePlan: Plano com sources mapeando cada searcher as suas queries,
                mais listas separadas de searchers primarios e secundarios.
        """
        domain_key = intent.domain.value
        # FASE 0.3: Se domínio identificado não estiver no mapa, use "universal"
        if domain_key not in self.domain_map:
            logger.info(
                "Domínio '%s' não encontrado em domains.yaml — usando 'universal' como fallback",
                domain_key,
            )
            domain_key = "universal"
        # Segundo fallback: se "universal" também não existir, use "general"
        if domain_key not in self.domain_map:
            domain_key = "general"

        # ── Roteamento por MODO de operação (M1 — modo 'mito') ───────────────
        # Quando o modo de operação define searchers explícitos, eles têm
        # prioridade sobre o mapeamento por domínio. Isso garante que modos
        # como 'mito' (fact-checking) direcionem a busca para as fontes
        # certas (web/Wikipedia/Snopes/Reddit) independentemente do domínio
        # detectado pelo IntentAnalyzer. O domínio detectado vira secundário
        # (complemento), preservando cobertura sem quebrar o comportamento
        # dos modos que já funcionavam (cirurgia, guerrilha, etc.).
        if self.mode:
            try:
                from src.operation_modes import OperationModes

                mode_cfg = OperationModes.get_mode(self.mode)
                mode_searchers = list(mode_cfg.searchers)
                if mode_searchers:
                    mapping = self.domain_map.get(domain_key, DOMAIN_SOURCES["general"])
                    domain_primary = list(mapping.get("primary", []))
                    domain_secondary = list(mapping.get("secondary", []))

                    # Primary = searchers do modo; secondary = domínio detectado
                    # (sem duplicar os já presentes no primary).
                    secondary = [
                        s
                        for s in domain_primary + domain_secondary
                        if s not in mode_searchers
                    ]
                    primary = mode_searchers

                    logger.info(
                        "SourcePlanner: modo '%s' sobrepôs fontes → primary=%s",
                        self.mode,
                        primary,
                    )

                    # Pula o roteamento defensivo de notícias e o LLM-router
                    # abaixo, pois o modo já definiu o plano explícito.
                    primary = self._apply_user_weights(primary, domain_key)
                    secondary = self._apply_user_weights(secondary, domain_key)

                    # Fase 2 — TrustRuleStore: aplica regras de allow/deny
                    if context is None:
                        mode_trust_rules = {}
                    elif isinstance(context, dict):
                        mode_trust_rules = context.get("extra", {}).get(
                            "trust_rules", {}
                        )
                    else:
                        mode_trust_rules = getattr(context, "extra", {}).get(
                            "trust_rules", {}
                        )
                    if mode_trust_rules:
                        denied = {
                            s for s, tier in mode_trust_rules.items() if tier == "deny"
                        }
                        allowed_priority = [
                            s for s, tier in mode_trust_rules.items() if tier == "allow"
                        ]
                        primary = [s for s in primary if s not in denied]
                        secondary = [s for s in secondary if s not in denied]
                        for s in reversed(allowed_priority):
                            if s not in primary:
                                primary.insert(0, s)

                    mode_plan: dict[str, list] = {}
                    for source in primary + secondary:
                        mode_plan[source] = self._select_queries_for_source(
                            queries, source, intent
                        )

                    return SourcePlan(
                        sources=mode_plan, primary=primary, secondary=secondary
                    )
            except Exception as e:
                logger.warning(
                    "SourcePlanner: falha ao aplicar modo '%s', usando roteamento por domínio: %s",
                    self.mode,
                    e,
                )

        # FASE 5 — Roteamento defensivo de notícias (Parte 4): se o
        # IntentAnalyzer classificou como GENERAL mas a query traz sinais
        # noticiosos explícitos e nenhuma palavra técnica, promove para o
        # domínio "news" (fontes de notícia em tempo real). Respeita a
        # classificação técnica quando há sobreposição (precisão primeiro).
        if domain_key in ("general", "universal"):
            query_lower = (queries[0].query if queries else "").lower()
            has_news = any(kw in query_lower for kw in NEWS_KEYWORDS)
            has_tech = any(kw in query_lower for kw in TECH_KEYWORDS)
            if has_news and not has_tech and "news" in self.domain_map:
                logger.info(
                    "SourcePlanner: query noticiosa '%s' roteada para domínio 'news'",
                    query_lower[:60],
                )
                domain_key = "news"

        # Roteamento dinâmico (LLM-driven) para o domínio universal ou quando
        # o domínio cai no fallback universal. Domínios técnicos mantêm o
        # roteamento estático (comportamento atual preservado).
        if domain_key == "universal" and self.llm is not None:
            return self._plan_universal_with_llm(intent, queries, domain_key, context)

        mapping = self.domain_map.get(domain_key, DOMAIN_SOURCES["general"])

        primary = mapping.get("primary", [])
        secondary = mapping.get("secondary", [])

        # Fase 4 — personalização: reordena conforme o histórico de fontes do usuário.
        primary = self._apply_user_weights(primary, domain_key)
        secondary = self._apply_user_weights(secondary, domain_key)

        # Fase 2 — TrustRuleStore: aplica regras de allow/deny do usuário
        if context is None:
            trust_rules: dict = {}
        elif isinstance(context, dict):
            trust_rules = context.get("extra", {}).get("trust_rules", {})
        else:
            trust_rules = getattr(context, "extra", {}).get("trust_rules", {})
        if trust_rules:
            denied = {s for s, tier in trust_rules.items() if tier == "deny"}
            allowed_priority = [s for s, tier in trust_rules.items() if tier == "allow"]

            # Remove fontes denylisted do plano
            primary = [s for s in primary if s not in denied]
            secondary = [s for s in secondary if s not in denied]

            # Promove fontes allowlisted para o topo de primary
            for s in reversed(allowed_priority):
                if s not in primary:
                    primary.insert(0, s)

        plan: dict[str, list[ExpandedQuery]] = {}
        for source in primary + secondary:
            plan[source] = self._select_queries_for_source(queries, source, intent)

        return SourcePlan(sources=plan, primary=primary, secondary=secondary)

    def _plan_universal_with_llm(
        self,
        intent: IntentResult,
        queries: list[ExpandedQuery],
        domain_key: str,
        context: dict = None,
    ) -> SourcePlan:
        """Planeia o dominio universal usando LLM + fallback em cascata.

        Tenta obter fontes via LLM (``_plan_with_llm``) e mescla o resultado
        com as fontes estaticas do yaml para ``universal``. Se o LLM falhar ou
        retornar vazio, usa as fontes do yaml como está.
        """
        yaml_mapping = self.domain_map.get(domain_key, {})
        yaml_primary = yaml_mapping.get("primary", [])
        yaml_secondary = yaml_mapping.get("secondary", [])

        llm_sources: list[str] = []
        try:
            llm_sources = self._run_async(
                self._plan_with_llm(intent, queries[0].query if queries else "")
            )
        except Exception as e:
            logger.warning("Universal Router LLM falhou: %s — usando yaml", e)

        # Mescla LLM (primário) com fontes do yaml (fallback/complemento)
        merged = list(llm_sources)
        for src in yaml_primary + yaml_secondary:
            if src not in merged:
                merged.append(src)

        # Se o LLM não retornou nada, garante ao menos as fontes do yaml
        if not llm_sources:
            merged = yaml_primary + yaml_secondary

        primary = merged[: max(len(yaml_primary), 1)] if llm_sources else yaml_primary
        secondary = [s for s in merged if s not in (primary or merged[:1])]

        # Fase 4 — personalização: reordena conforme o histórico de fontes do usuário.
        primary = self._apply_user_weights(primary, domain_key)
        secondary = self._apply_user_weights(secondary, domain_key)

        # Fase 2 — TrustRuleStore: aplica regras de allow/deny do usuário
        if context is None:
            trust_rules: dict = {}
        elif isinstance(context, dict):
            trust_rules = context.get("extra", {}).get("trust_rules", {})
        else:
            trust_rules = getattr(context, "extra", {}).get("trust_rules", {})
        if trust_rules:
            denied = {s for s, tier in trust_rules.items() if tier == "deny"}
            allowed_priority = [s for s, tier in trust_rules.items() if tier == "allow"]

            # Remove fontes denylisted do plano
            primary = [s for s in primary if s not in denied]
            secondary = [s for s in secondary if s not in denied]

            # Promove fontes allowlisted para o topo de primary
            for s in reversed(allowed_priority):
                if s not in primary:
                    primary.insert(0, s)

        plan: dict[str, list[ExpandedQuery]] = {}
        for source in primary + secondary:
            plan[source] = self._select_queries_for_source(queries, source, intent)

        return SourcePlan(sources=plan, primary=primary, secondary=secondary)

    async def _plan_with_llm(self, intent: IntentResult, query: str) -> list[str]:
        """Roteamento dinâmico via LLM para o dominio universal.

        Lê o prompt de ``prompts/source_planner.md`` (com fallback para
        :data:`UNIVERSAL_PLANNER_PROMPT` se o arquivo nao existir) e pede ao LLM
        a lista de 3-6 fontes mais relevantes. A resposta é validada contra as
        fontes realmente registradas no ``SearcherFactory`` — nomes inexistentes
        são descartados silenciosamente.

        Args:
            intent: Resultado da analise de intencao (dominio + intencao).
            query: Query original do usuario.

        Returns:
            list[str]: Nomes de searchers validos sugeridos pelo LLM. Lista
                vazia se o LLM falhar ou nao retornar fontes validas.
        """
        if self.llm is None:
            logger.debug("Sem LLM configurado — Universal Router pulado")
            return []

        # Lista de fontes realmente disponíveis no SearcherFactory
        try:
            from src.search.factory import SearcherFactory

            available = set(SearcherFactory.get_available_searchers())
        except Exception:
            available = set()

        # Carrega o prompt (arquivo ou fallback embutido)
        prompt = self._load_planner_prompt().format(
            query=query,
            domain=getattr(intent.domain, "value", str(intent.domain)),
            intent=getattr(intent, "intent", ""),
            available_sources=", ".join(sorted(available))
            if available
            else "wikipedia, duckduckgo, searxng, web, reddit, hackernews, github, arxiv",
        )

        try:
            response = await self.llm.generate(prompt)
        except Exception as e:
            logger.warning("Falha na geracao LLM do SourcePlanner: %s", e)
            return []

        return self._parse_llm_sources(response, available)

    def _load_planner_prompt(self) -> str:
        """Retorna o prompt do Universal Router.

        Lê ``prompts/source_planner.md`` se existir; caso contrario usa o
        :data:`UNIVERSAL_PLANNER_PROMPT` embutido.
        """
        prompt_path = Path(__file__).parent.parent / "prompts" / "source_planner.md"
        if prompt_path.exists():
            try:
                return prompt_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Erro ao ler prompts/source_planner.md: %s", e)
        return UNIVERSAL_PLANNER_PROMPT

    @staticmethod
    def _parse_llm_sources(response: str, available: set[str]) -> list[str]:
        """Extrai e valida nomes de searchers da resposta do LLM.

        Aceita tanto formato CSV simples quanto tenta tolerar ruido (markdown,
        bullets). Remove duplicatas e nomes que nao existem no ``available``.
        """
        if not response:
            return []

        # Tolerância: pega só a primeira linha se houver quebras
        first_line = response.strip().splitlines()[0] if response.strip() else ""
        # Remove bullets/markdown comuns
        cleaned = first_line.replace("- ", "").replace("* ", "").strip()
        parts = [p.strip().lower() for p in cleaned.split(",") if p.strip()]

        seen: set[str] = set()
        result: list[str] = []
        for name in parts:
            name = name.strip().strip("`").strip()
            if not name or name in seen:
                continue
            if available and name not in available:
                continue
            seen.add(name)
            result.append(name)
        return result

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
