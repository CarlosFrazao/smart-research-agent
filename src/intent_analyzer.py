"""Analisador de intencao para classificar queries de pesquisa por dominio e objetivo.

Usa heuristicas de palavras-chave para classificar rapidamente o dominio e
a intencao da query, reduzindo chamadas LLM. Quando necessario, usa o LLM
para enriquecer a analise com entidades e urgencia detectadas.

Tambem expoe `analyze_and_expand()`, que consolida a classificacao de intencao
e a expansao de queries (normalmente 2 chamadas LLM sequenciais) em, no
maximo, 1 unica chamada — combinada com um cache de intencao por similaridade
para evitar reclassificar queries repetidas ou muito parecidas.
"""

import logging
import re
import time
from collections import deque

from src.clients.llm_client import LLMClient
from src.query_expander import QueryExpander
from src.types import Domain, ExpandedQuery, Intention, IntentResult

logger = logging.getLogger(__name__)

DOMAIN_KEYWORDS = {
    Domain.SAAS_B2B: [
        "crm",
        "erp",
        "helpdesk",
        "marketing",
        "saas",
        "b2b",
        "sales",
        "support",
    ],
    Domain.DEV_TOOLS: [
        "ide",
        "linter",
        "ci/cd",
        "testing",
        "debugger",
        "git",
        "vscode",
        "editor",
    ],
    Domain.AI_ML: [
        "llm",
        "model",
        "ai",
        "ml",
        "neural",
        "transformer",
        "gpt",
        "claude",
        "embedding",
    ],
    Domain.AUTOMATION: [
        "n8n",
        "zapier",
        "make",
        "rpa",
        "workflow",
        "automation",
        "pipeline",
    ],
    Domain.INFRASTRUCTURE: [
        "docker",
        "kubernetes",
        "k8s",
        "cloud",
        "serverless",
        "terraform",
        "aws",
    ],
    Domain.OPEN_SOURCE: [
        "github",
        "open source",
        "library",
        "framework",
        "package",
        "npm",
        "pypi",
    ],
}

INTENTION_KEYWORDS = {
    Intention.COMPARE: [
        "compare",
        "vs",
        "versus",
        "better than",
        "alternative to",
        "difference",
    ],
    Intention.LEARN: ["how does", "what is", "how to", "tutorial", "explain", "guide"],
    Intention.IMPLEMENT: [
        "install",
        "setup",
        "deploy",
        "configure",
        "self-host",
        "docker run",
    ],
    Intention.EVALUATE: ["worth it", "pros and cons", "review", "should i use", "good"],
}


class IntentAnalyzer:
    """Classifica queries de pesquisa por dominio, intencao, urgencia e entidades.

    Usa heuristicas de palavras-chave (custo zero) como curto-circuito antes
    de chamar o LLM, para economizar tokens em queries com sinais claros.

    Alem do fluxo classico (`analyze()`), expoe `analyze_and_expand()`, que
    combina intent + query expansion em 1 unica chamada LLM (em vez de 2
    chamadas sequenciais) e mantem um cache em memoria de intencoes recentes,
    reaproveitado para queries semanticamente similares.
    """

    # ── Cache de intencao por similaridade ──────────────────────────────────
    _CACHE_MAX_SIZE = 256
    _CACHE_TTL_SECONDS = 3600
    _CACHE_SIMILARITY_THRESHOLD = 0.8
    _STOPWORDS = {
        "the", "a", "an", "of", "for", "and", "or", "is", "are", "to", "in",
        "on", "with", "how", "what", "best", "vs",
        "de", "da", "do", "das", "dos", "um", "uma", "para", "com", "como",
        "melhor", "melhores", "que", "qual",
    }

    def __init__(
        self,
        llm_client: LLMClient,
        query_expander: QueryExpander | None = None,
    ):
        self.llm = llm_client
        # Reaproveita um QueryExpander compartilhado (ex.: o do orquestrador)
        # quando fornecido, para nao duplicar instancias/estado.
        self.query_expander = query_expander or QueryExpander(llm_client)
        # Cache FIFO limitado: (tokens_normalizados, IntentResult, timestamp).
        self._intent_cache: deque[tuple[frozenset[str], IntentResult, float]] = deque(
            maxlen=self._CACHE_MAX_SIZE
        )

    def _heuristic_domain(self, query: str) -> Domain:
        """Classifica o dominio da query usando correspondencia de palavras-chave.

        Args:
            query: Query do usuario.

        Returns:
            Domain: Dominio detectado, ou `Domain.GENERAL` se nenhum sinal encontrado.
        """
        query_lower = query.lower()
        scores = {domain: 0 for domain in Domain}
        for domain, keywords in DOMAIN_KEYWORDS.items():
            for kw in keywords:
                if kw in query_lower:
                    scores[domain] += 1
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else Domain.GENERAL

    def _heuristic_intention(self, query: str) -> Intention:
        """Detecta a intencao principal da query usando palavras-chave.

        Args:
            query: Query do usuario.

        Returns:
            Intention: Intencao detectada, ou `Intention.DISCOVER` como padrao.
        """
        query_lower = query.lower()
        for intention, keywords in INTENTION_KEYWORDS.items():
            for kw in keywords:
                if kw in query_lower:
                    return intention
        return Intention.DISCOVER

    def _heuristic_urgency(self, query: str) -> str:
        """Detecta se a query tem carater de urgencia ou busca por novidades recentes.

        Args:
            query: Query do usuario.

        Returns:
            str: ``"sim"`` se a query indica urgencia ou recencia, ``"nao"`` caso contrario.
        """
        urgent = [
            "2026",
            "2025",
            "new",
            "latest",
            "trending",
            "recent",
            "now",
            "this year",
        ]
        return "sim" if any(u in query.lower() for u in urgent) else "nao"

    def _extract_entities_heuristic(self, query: str) -> list[str]:
        """Extrai entidades da query usando expressoes regulares.

        Captura palavras em CamelCase e padroes `org/repo` do GitHub.

        Args:
            query: Query do usuario.

        Returns:
            list[str]: Lista deduplicada de entidades identificadas.
        """
        entities = re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", query)
        repos = re.findall(r"\b[\w-]+/[\w-]+\b", query)
        return list(set(entities + repos))

    # ── Cache de intencao por similaridade ──────────────────────────────────

    def _normalize_tokens(self, query: str) -> frozenset[str]:
        """Extrai um conjunto de tokens significativos para comparar queries.

        Usado como assinatura para similaridade de Jaccard: minusculas,
        remove pontuacao, descarta palavras curtas e stopwords comuns.

        Args:
            query: Query a normalizar.

        Returns:
            frozenset[str]: Conjunto de tokens (vazio se nada relevante restar).
        """
        words = re.findall(r"[a-z0-9]+", query.lower())
        return frozenset(w for w in words if len(w) > 2 and w not in self._STOPWORDS)

    @staticmethod
    def _jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
        """Calcula a similaridade de Jaccard entre dois conjuntos de tokens."""
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _lookup_cached_intent(self, query: str) -> IntentResult | None:
        """Busca no cache um `IntentResult` para uma query igual ou similar.

        Percorre o cache (mais recentes primeiro) descartando entradas
        expiradas e retorna a primeira cujo conjunto de tokens tenha
        similaridade de Jaccard >= `_CACHE_SIMILARITY_THRESHOLD` com a query.

        Args:
            query: Query do usuario (idealmente ja enriquecida com contexto).

        Returns:
            IntentResult | None: Resultado reaproveitado, ou None se nao houver hit.
        """
        tokens = self._normalize_tokens(query)
        if not tokens:
            return None
        now = time.monotonic()
        for cached_tokens, cached_result, cached_at in reversed(self._intent_cache):
            if now - cached_at > self._CACHE_TTL_SECONDS:
                continue
            similarity = self._jaccard_similarity(tokens, cached_tokens)
            if similarity >= self._CACHE_SIMILARITY_THRESHOLD:
                logger.debug(
                    f"IntentAnalyzer: cache hit por similaridade ({similarity:.2f}) "
                    f"para query='{query}'"
                )
                return cached_result
        return None

    def _store_cached_intent(self, query: str, result: IntentResult) -> None:
        """Armazena um `IntentResult` no cache, indexado pelos tokens da query."""
        tokens = self._normalize_tokens(query)
        if tokens:
            self._intent_cache.append((tokens, result, time.monotonic()))

    async def analyze(self, query: str, force_llm: bool = False) -> IntentResult:
        """Analisa a intencao da query e retorna um `IntentResult` estruturado.

        Usa curto-circuito heuristico para evitar chamada LLM quando o dominio
        ou intencao for detectado com confianca suficiente, e um cache de
        similaridade para reaproveitar classificacoes de queries parecidas.

        Args:
            query: Query do usuario a ser analisada.
            force_llm: Se True, forca a chamada LLM mesmo quando a heuristica
                e suficientemente confiante ou ha um hit no cache. Util para testes.

        Returns:
            IntentResult: Objeto com dominio, intencao, urgencia e entidades detectadas.
        """
        if not force_llm:
            cached = self._lookup_cached_intent(query)
            if cached is not None:
                return cached

        domain = self._heuristic_domain(query)
        intention = self._heuristic_intention(query)
        urgency = self._heuristic_urgency(query)
        entities = self._extract_entities_heuristic(query)

        # ── Curto-circuito heurístico (economiza chamada LLM) ──────────────
        # Se domínio não é GENERAL e a intenção foi detectada com clareza,
        # as heurísticas já são suficientes — pula o LLM para evitar rate-limit.
        heuristic_is_confident = (
            domain != Domain.GENERAL
            or intention != Intention.DISCOVER
            or len(entities) >= 1
        )
        if heuristic_is_confident and not force_llm:
            logger.debug(
                f"IntentAnalyzer: curto-circuito heurístico ativo "
                f"(domain={domain.value}, intention={intention.value}, entities={entities}). "
                "Chamada LLM omitida."
            )
            result = IntentResult(
                domain=domain,
                entities=entities,
                intention=intention,
                urgency=urgency,
                confidence="media",
            )
            self._store_cached_intent(query, result)
            return result

        prompt_text = (
            "Voce e um analisador de intencao especializado em tecnologia.\n"
            "Analise a query e classifique em JSON:\n\n"
            f"Query: {query}\n"
            f"Heuristica inicial: dominio={domain.value}, intencao={intention.value}, urgencia={urgency}\n\n"
            "Responda em JSON valido:\n"
            "{\n"
            f'  "domain": "{domain.value}",\n'
            f'  "entities": {entities},\n'
            f'  "intention": "{intention.value}",\n'
            f'  "urgency": "{urgency}",\n'
            '  "confidence": "alta|media|baixa"\n'
            "}\n\n"
            "Regras:\n"
            "- DOMAIN: saas_b2b, dev_tools, ai_ml, automation, infrastructure, open_source, general\n"
            "- ENTITIES: nomes de produtos, empresas, tecnologias\n"
            "- INTENCAO: discover, compare, learn, implement, evaluate\n"
            "- URGENCIA: sim (se menciona 2026, novo, trending) ou nao\n"
        )

        schema = {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "entities": {"type": "array", "items": {"type": "string"}},
                "intention": {"type": "string"},
                "urgency": {"type": "string"},
                "confidence": {"type": "string"},
            },
            "required": ["domain", "entities", "intention", "urgency", "confidence"],
        }

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
            intent_result = IntentResult(
                domain=Domain(result.get("domain", domain.value)),
                entities=result.get("entities", entities),
                intention=Intention(result.get("intention", intention.value)),
                urgency=result.get("urgency", urgency),
                confidence=result.get("confidence", "media"),
            )
        except Exception as e:
            logger.warning(f"LLM intent analysis falhou, usando heuristica: {e}")
            intent_result = IntentResult(
                domain=domain,
                entities=entities,
                intention=intention,
                urgency=urgency,
                confidence="media",
            )

        self._store_cached_intent(query, intent_result)
        return intent_result

    # ── Fluxo consolidado: intent + query expansion em 1 chamada LLM ────────

    async def analyze_and_expand(
        self,
        query: str,
        context_query: str | None = None,
        force_llm: bool = False,
    ) -> tuple[IntentResult, list[ExpandedQuery]]:
        """Executa intent + expansao de queries em, no maximo, 1 chamada LLM.

        Substitui o fluxo anterior de 2 chamadas sequenciais — uma para
        classificar a intencao (`analyze`) e outra para expandir a query
        (`QueryExpander.expand`) — por uma unica chamada combinada, sempre
        que uma chamada LLM for necessaria. Quando ha um hit no cache de
        intencao (query igual ou suficientemente similar a uma ja
        classificada), a classificacao de intencao e reaproveitada sem custo
        adicional, e a chamada ao LLM (se houver) so precisa gerar as queries
        expandidas.

        Args:
            query: Query original do usuario, usada para gerar as expansoes.
            context_query: Query enriquecida com contexto (ex.: memoria de
                pesquisas anteriores), usada para classificar a intencao. Se
                None, usa `query`.
            force_llm: Se True, ignora cache e curto-circuito heuristico e
                forca a chamada LLM combinada.

        Returns:
            tuple[IntentResult, list[ExpandedQuery]]: Intencao classificada e
            lista de queries expandidas (LLM ou fallback deterministico).
        """
        intent_query = context_query or query

        cached_intent = None if force_llm else self._lookup_cached_intent(intent_query)

        domain = self._heuristic_domain(intent_query)
        intention = self._heuristic_intention(intent_query)
        urgency = self._heuristic_urgency(intent_query)
        entities = self._extract_entities_heuristic(intent_query)
        heuristic_is_confident = (
            domain != Domain.GENERAL
            or intention != Intention.DISCOVER
            or len(entities) >= 1
        )

        if cached_intent is not None:
            intent = cached_intent
            need_intent_llm = False
        else:
            intent = IntentResult(
                domain=domain,
                entities=entities,
                intention=intention,
                urgency=urgency,
                confidence="media",
            )
            need_intent_llm = force_llm or not heuristic_is_confident

        prompt_text, schema = self._build_combined_prompt(
            query, intent, include_intent=need_intent_llm
        )

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
        except Exception as e:
            logger.warning(
                f"IntentAnalyzer.analyze_and_expand: chamada LLM combinada "
                f"falhou, usando fallback heuristico/deterministico: {e}"
            )
            self._store_cached_intent(intent_query, intent)
            return intent, self.query_expander.fallback_expand(query, intent)

        if need_intent_llm:
            intent = IntentResult(
                domain=Domain(result.get("domain", domain.value)),
                entities=result.get("entities", entities),
                intention=Intention(result.get("intention", intention.value)),
                urgency=result.get("urgency", urgency),
                confidence=result.get("confidence", "media"),
            )

        self._store_cached_intent(intent_query, intent)

        queries_payload = result.get("queries") or []
        try:
            expanded = (
                [ExpandedQuery(**q) for q in queries_payload]
                if queries_payload
                else self.query_expander.fallback_expand(query, intent)
            )
        except Exception as e:
            logger.warning(
                f"IntentAnalyzer.analyze_and_expand: parse das queries "
                f"expandidas falhou, usando fallback: {e}"
            )
            expanded = self.query_expander.fallback_expand(query, intent)

        return intent, expanded

    def _build_combined_prompt(
        self, query: str, intent: IntentResult, include_intent: bool
    ) -> tuple[str, dict]:
        """Monta o prompt e o schema JSON combinados para intent + expansion.

        Args:
            query: Query original do usuario.
            intent: Intencao ja conhecida (heuristica ou cache), usada como
                contexto e como base quando `include_intent` for False.
            include_intent: Se True, o prompt tambem pede a classificacao
                completa de intencao (dominio/entidades/intencao/urgencia/
                confianca); se False, pede apenas as queries expandidas.

        Returns:
            tuple[str, dict]: Prompt textual e schema JSON esperado da resposta.
        """
        intent_task = ""
        intent_schema_props: dict = {}
        if include_intent:
            intent_task = (
                "1. Classifique a intencao da query em JSON.\n"
                f"   Heuristica inicial: dominio={intent.domain.value}, "
                f"intencao={intent.intention.value}, urgencia={intent.urgency}\n"
                "   Regras: DOMAIN em [saas_b2b, dev_tools, ai_ml, automation, "
                "infrastructure, open_source, general]; INTENCAO em [discover, "
                "compare, learn, implement, evaluate]; URGENCIA sim (se menciona "
                "2026, novo, trending) ou nao; ENTITIES = nomes de produtos, "
                "empresas, tecnologias.\n"
            )
            intent_schema_props = {
                "domain": {"type": "string"},
                "entities": {"type": "array", "items": {"type": "string"}},
                "intention": {"type": "string"},
                "urgency": {"type": "string"},
                "confidence": {"type": "string"},
            }

        expansion_step = "2" if include_intent else "1"
        expansion_task = (
            f"{expansion_step}. Gere entre 8 e 12 queries expandidas que maximizam "
            "a cobertura de informacao, aplicando: variacoes de terminologia "
            "(sinonimos tecnicos, abreviacoes), perspectivas diferentes "
            "(implementacao, comparacao, critica, casos de uso), queries de "
            "evidencia (benchmarks, reviews, dados reais) e queries de "
            "comunidade (Reddit, HN).\n"
            "   - NAO gere queries que retornariam os mesmos resultados da "
            "query original\n"
            "   - Prioridade alta = pesquisar primeiro, baixa = pesquisar por "
            "ultimo\n"
            "   - Use ingles para termos tecnicos (melhor cobertura no "
            "GitHub/HN)\n"
        )

        intent_json_fields = (
            '  "domain": "string",\n'
            '  "entities": ["string"],\n'
            '  "intention": "string",\n'
            '  "urgency": "string",\n'
            '  "confidence": "alta|media|baixa",\n'
            if include_intent
            else ""
        )

        prompt_text = (
            "Voce e um especialista em analise de intencao e expansao de "
            "queries de pesquisa tecnologica.\n\n"
            f"Query original: {query}\n"
            f"Dominio conhecido: {intent.domain.value}\n"
            f"Entidades conhecidas: {', '.join(intent.entities) or 'nenhuma'}\n\n"
            "Tarefas:\n"
            f"{intent_task}"
            f"{expansion_task}\n"
            "Responda em JSON valido:\n"
            "{\n"
            f"{intent_json_fields}"
            '  "queries": [\n'
            "    {\n"
            '      "query": "string",\n'
            '      "type": "synonym|perspective|evidence|community|academic",\n'
            '      "priority": "alta|media|baixa",\n'
            '      "rationale": "por que esta variacao e util"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )

        schema = {
            "type": "object",
            "properties": {
                **intent_schema_props,
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "type": {"type": "string"},
                            "priority": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["query", "type", "priority", "rationale"],
                    },
                },
            },
            "required": [*intent_schema_props.keys(), "queries"],
        }
        return prompt_text, schema
