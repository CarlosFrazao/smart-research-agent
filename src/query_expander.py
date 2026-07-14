"""Expansor de queries usando LLM para maximizar cobertura de fontes.

Gera variações inteligentes da query original aplicando estrategias de
sinonimos, perspectivas, evidencias, comunidades e queries academicas.
"""

import logging

from src.clients.llm_client import LLMClient
from src.types import Domain, ExpandedQuery, IntentResult

logger = logging.getLogger(__name__)

# Domínios em que a estratégia de queries acadêmicas (arxiv, survey, dataset)
# faz sentido. Mantido como constante de módulo (em vez de hardcoded dentro
# do prompt) para não precisar tocar em duas classes toda vez que o enum
# `Domain` ganhar um novo valor relevante para pesquisa acadêmica.
ACADEMIC_DOMAINS = {Domain.AI_ML}

# Faixa de tamanho exigida tanto do resultado via LLM quanto do fallback
# deterministico — usada para top-up (poucos itens) e truncamento (itens demais).
MIN_EXPANSIONS = 8
MAX_EXPANSIONS = 12

# Mapa de normalização para prioridades fora do vocabulário esperado
# (alta/media/baixa). LLMs — sobretudo em failover para providers diferentes —
# tendem a devolver "high/medium/low" ou variações acentuadas mesmo quando
# instruídos em português; sem isso, um único item fora do padrão derruba a
# lista inteira via ValidationError (ExpandedQuery.priority é Literal).
_PRIORITY_ALIASES = {
    "alta": "alta",
    "alto": "alta",
    "high": "alta",
    "média": "media",
    "media": "media",
    "medio": "media",
    "médio": "media",
    "medium": "media",
    "mid": "media",
    "baixa": "baixa",
    "baixo": "baixa",
    "low": "baixa",
}


def _normalize_priority(value: object) -> str:
    """Normaliza um valor de prioridade arbitrário para alta/media/baixa.

    Qualquer valor não reconhecido (tipo errado, string vazia, vocabulário
    inesperado) cai em ``"media"`` em vez de propagar erro — a prioridade é
    um hint de ordenação, não um dado crítico o suficiente para descartar
    uma query expandida inteira.
    """
    if not isinstance(value, str):
        return "media"
    return _PRIORITY_ALIASES.get(value.strip().lower(), "media")


def _normalize_key(query_text: str) -> str:
    """Chave de deduplicação: minúsculas + espaços colapsados."""
    return " ".join(query_text.split()).lower()


class QueryExpander:
    """Expande queries de pesquisa usando LLM para maximar cobertura de informacao.

    Gera entre 8 e 12 queries expandidas com tipos distintos (sinonimo,
    perspectiva, evidencia, comunidade, academica) e niveis de prioridade.

    O `SourcePlanner` roteia cada `ExpandedQuery` para searchers específicos
    filtrando por `type` (ex.: apenas queries `type="academic"` chegam ao
    Arxiv com prioridade). Por isso tanto o prompt do LLM quanto o
    `fallback_expand` determinístico precisam cobrir, sempre que fizer
    sentido para o domínio, os tipos: ``synonym``, ``perspective``,
    ``evidence``, ``community``, ``academic``, além dos tipos auxiliares
    ``qualificador``, ``temporal``, ``comparacao``, ``plataforma``,
    ``caso_de_uso`` e ``original``.
    """

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    async def expand(self, query: str, intent: IntentResult) -> list[ExpandedQuery]:
        """Gera queries expandidas via LLM para ampliar a cobertura da pesquisa.

        Envia um prompt estruturado ao LLM com a query e contexto de intencao,
        parseia o JSON retornado e converte em `ExpandedQuery`. Itens
        individualmente invalidos (ex: prioridade fora do vocabulario
        esperado) sao normalizados ou descartados sem derrubar a lista
        inteira. O resultado final e deduplicado e ajustado para ficar
        entre `MIN_EXPANSIONS` e `MAX_EXPANSIONS` itens, completando com
        `fallback_expand` se o LLM devolver poucas queries validas.

        Em caso de falha total do LLM (erro de rede, rate-limit, JSON
        invalido) ou de nenhuma query valida sobrar apos o parse, cai
        integralmente para `fallback_expand`, que gera expansoes
        deterministicas cobrindo os mesmos tipos.

        Args:
            query: Query original do usuario.
            intent: Resultado da analise de intencao com dominio e entidades.

        Returns:
            list[ExpandedQuery]: Lista de queries expandidas ordenadas por prioridade.
        """
        prompt_text = self._build_prompt(query, intent)
        schema = self._SCHEMA

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
            raw_queries = result.get("queries", []) or []
        except Exception as e:
            logger.warning(f"LLM query expansion falhou, usando fallback: {e}")
            return self.fallback_expand(query, intent)

        parsed = self._parse_llm_queries(raw_queries)
        deduped = self._dedupe(parsed, original_query=query)

        if not deduped:
            logger.warning(
                "LLM query expansion nao retornou nenhuma query valida, usando fallback"
            )
            return self.fallback_expand(query, intent)

        if len(deduped) < MIN_EXPANSIONS:
            deduped = self._top_up(deduped, query, intent)

        return deduped[:MAX_EXPANSIONS]

    # ── Construção do prompt ──────────────────────────────────────────────

    def _build_prompt(self, query: str, intent: IntentResult) -> str:
        """Monta o prompt de expansão, incluindo a estratégia academica
        apenas quando o dominio da query justificar (ver `ACADEMIC_DOMAINS`).
        """
        strategies = (
            "1. Variações de terminologia (sinônimos técnicos, abreviações)\n"
            "2. Perspectivas diferentes (implementação, comparação, crítica, casos de uso)\n"
            "3. Queries de evidência (benchmarks, reviews, dados reais)\n"
            "4. Queries de comunidade (Reddit, HN)\n"
        )
        if intent.domain in ACADEMIC_DOMAINS:
            strategies += (
                "5. Queries acadêmicas (arxiv, survey paper, benchmarks/datasets — "
                "domínio de pesquisa/ML detectado)\n"
            )

        return (
            "Você é um especialista em expansão de queries de pesquisa.\n"
            "Gere variações inteligentes que maximizam a cobertura de informação.\n\n"
            "Estratégias a aplicar:\n"
            f"{strategies}\n"
            "Regras:\n"
            f"- Gere entre {MIN_EXPANSIONS} e {MAX_EXPANSIONS} queries (nem menos, nem mais)\n"
            "- NÃO gere queries que retornariam os mesmos resultados da query original\n"
            "- NÃO repita a mesma query (mesmo com pequenas variações de grafia) mais de uma vez\n"
            "- Prioridade alta = pesquisar primeiro, baixa = pesquisar por último\n"
            "- Use inglês para termos técnicos (melhor cobertura no GitHub/HN)\n\n"
            f"Query original: {query}\n"
            f"Dominio: {intent.domain.value}\n"
            f"Entidades: {', '.join(intent.entities)}\n\n"
            "Responda em JSON válido:\n"
            "{\n"
            '  "queries": [\n'
            "    {\n"
            '      "query": "string",\n'
            '      "type": "synonym|perspective|evidence|community|academic",\n'
            '      "priority": "alta|media|baixa",\n'
            '      "rationale": "por que esta variação é útil"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )

    _SCHEMA = {
        "type": "object",
        "properties": {
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
            }
        },
        "required": ["queries"],
    }

    # ── Parsing resiliente do retorno do LLM ─────────────────────────────

    def _parse_llm_queries(self, raw_queries: list) -> list[ExpandedQuery]:
        """Converte o payload bruto do LLM em `ExpandedQuery`, item a item.

        Um item malformado (campo faltando, tipo errado) e descartado
        individualmente via log de warning, em vez de descartar a lista
        inteira — o comportamento antigo (`[ExpandedQuery(**q) for q in
        queries]` sem try/except por item) fazia uma unica query mal
        formada pelo LLM jogar fora todas as outras 7-11 validas.
        """
        parsed: list[ExpandedQuery] = []
        for item in raw_queries:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["priority"] = _normalize_priority(item.get("priority"))
            try:
                parsed.append(ExpandedQuery(**item))
            except Exception as e:
                logger.debug(
                    f"Descartando query expandida invalida do LLM: {item} ({e})"
                )
        return parsed

    def _dedupe(
        self, queries: list[ExpandedQuery], original_query: str
    ) -> list[ExpandedQuery]:
        """Remove duplicatas (case/espaço-insensitivas) entre si e contra a
        query original, preservando a ordem de prioridade retornada pelo LLM.
        """
        seen = {_normalize_key(original_query)}
        unique: list[ExpandedQuery] = []
        for q in queries:
            key = _normalize_key(q.query)
            if key in seen:
                continue
            seen.add(key)
            unique.append(q)
        return unique

    def _top_up(
        self, queries: list[ExpandedQuery], query: str, intent: IntentResult
    ) -> list[ExpandedQuery]:
        """Completa uma lista curta demais (LLM devolveu < MIN_EXPANSIONS
        queries validas) com expansoes deterministicas, sem duplicar.
        """
        seen = {_normalize_key(q.query) for q in queries}
        seen.add(_normalize_key(query))
        topped_up = list(queries)
        for candidate in self.fallback_expand(query, intent):
            if len(topped_up) >= MIN_EXPANSIONS:
                break
            key = _normalize_key(candidate.query)
            if key in seen:
                continue
            seen.add(key)
            topped_up.append(candidate)
        return topped_up

    # ── Fallback determinístico (sem LLM) ────────────────────────────────

    def fallback_expand(self, query: str, intent: IntentResult) -> list[ExpandedQuery]:
        """Gera expansoes deterministicas (sem LLM) a partir de qualificadores fixos.

        Usado tanto pelo fallback interno de `expand()` quanto pelo fluxo
        consolidado do `IntentAnalyzer.analyze_and_expand()` quando a chamada
        LLM combinada falha ou nao retorna queries.

        Cobre deliberadamente os tipos ``community``, ``evidence``,
        ``caso_de_uso`` e (quando o dominio for academico) ``academic``
        alem dos ja existentes ``original``/``qualificador``/``temporal``/
        ``comparacao``/``plataforma``. Sem isso, o `SourcePlanner` fica sem
        nenhuma query do tipo esperado para rotear a Reddit, Hacker News,
        StackOverflow e Arxiv sempre que o LLM falha (rate-limit, timeout,
        JSON invalido) — o unico cenario em que este metodo e chamado nao
        e um caso raro, e sim o caminho de recuperacao mais comum do
        pipeline em produção.

        Args:
            query: Query original do usuario.
            intent: Resultado da analise de intencao (usado para dominio e entidades).

        Returns:
            list[ExpandedQuery]: Entre 8 e 12 expansoes deterministicas.
        """
        base = query.lower().strip()
        expansions = [
            ExpandedQuery(
                query=base, type="original", priority="alta", rationale="query original"
            ),
            ExpandedQuery(
                query=f"open source {base}",
                type="qualificador",
                priority="alta",
                rationale="encontra projetos open source",
            ),
            ExpandedQuery(
                query=f"self hosted {base}",
                type="qualificador",
                priority="alta",
                rationale="encontra alternativas self-hosted",
            ),
            ExpandedQuery(
                query=f"best {base} 2026",
                type="temporal",
                priority="media",
                rationale="resultados recentes",
            ),
            ExpandedQuery(
                query=f"{base} alternative",
                type="comparacao",
                priority="media",
                rationale="encontra alternativas",
            ),
            ExpandedQuery(
                query=f"github {base}",
                type="plataforma",
                priority="media",
                rationale="busca direta no GitHub",
            ),
            ExpandedQuery(
                query=f"{base} reddit discussion",
                type="community",
                priority="media",
                rationale="opinioes e discussoes de comunidade no Reddit",
            ),
            ExpandedQuery(
                query=f"{base} hacker news",
                type="community",
                priority="media",
                rationale="discussao tecnica na comunidade do Hacker News",
            ),
            ExpandedQuery(
                query=f"{base} benchmark performance",
                type="evidence",
                priority="media",
                rationale="dados objetivos de desempenho, nao opiniao",
            ),
            ExpandedQuery(
                query=f"{base} production use cases",
                type="caso_de_uso",
                priority="baixa",
                rationale="casos de uso reais em producao",
            ),
        ]

        if intent.domain in ACADEMIC_DOMAINS:
            expansions.append(
                ExpandedQuery(
                    query=f"{base} arxiv paper",
                    type="academic",
                    priority="baixa",
                    rationale="literatura academica/pesquisa relacionada",
                )
            )

        for entity in intent.entities[:2]:
            if len(expansions) >= MAX_EXPANSIONS:
                break
            expansions.append(
                ExpandedQuery(
                    query=f"alternative to {entity}",
                    type="comparacao",
                    priority="media",
                    rationale=f"comparacao direta com {entity}",
                )
            )

        return expansions[:MAX_EXPANSIONS]

    # Alias privado mantido por compatibilidade retroativa (nome anterior do metodo).
    _fallback_expand = fallback_expand
