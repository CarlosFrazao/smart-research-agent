"""Módulo STORM Perspectives Generator.

Simula múltiplos especialistas/personas (STORM style) para gerar
direcionamentos de busca a partir de visões complementares e profundas.

Papel na ferramenta
--------------------
Implementa a etapa de "multi-perspective question asking" do estilo STORM:
antes de aprofundar a pesquisa, simula um painel de especialistas com ângulos
complementares e não sobrepostos, cada um produzindo sub-queries direcionadas.
Isso é ortogonal a:

- `QueryExpander` (src/query_expander.py): gera variações lexicais/de tipo
  (sinônimo, evidência, comunidade...) de UMA query.
- `DeepResearcher` (src/deep_researcher.py): aprofunda hipóteses em árvore
  com beam search, a partir de um conjunto inicial de queries.

`StormPerspectiveGenerator` deveria semear esse conjunto inicial de queries
por perspectiva, ANTES da árvore de pesquisa. No estado atual do
repositório ele não é chamado por nenhum stage do pipeline
(`src/pipeline/stage_factory.py` não tem um "storm" em `DEFAULT_STAGE_NAMES`)
nem pelo `DeepResearcher` — só é exercitado pelos próprios testes. Vale
criar um `StormStage` (nos moldes de `expand_stage.py`) ou injetá-lo dentro
de `_generate_hypotheses` do `DeepResearcher` para deixar de ser código morto.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from src.cache import Cache
from src.clients.llm_client import LLMClient
from src.token_economy import TokenEconomy

logger = logging.getLogger("storm-perspectives")

# Perspectivas mudam pouco para o mesmo tópico — cache generoso reduz custo
# de chamadas LLM redundantes (mesmo padrão usado em report_generator.py).
_CACHE_TTL_SECONDS = 6 * 3600
_MIN_PERSPECTIVES = 1
_MAX_PERSPECTIVES = 6


class StormPerspectiveGenerator:
    """Gera personas especialistas (perspectivas) e sub-queries direcionadas para um tópico."""

    def __init__(self, llm_client: LLMClient, cache: Optional[Cache] = None) -> None:
        self.llm = llm_client
        # Cache opcional (injeção de dependência): quando o generator for
        # ligado ao pipeline, passar a mesma instância de `Cache` compartilhada
        # pelos demais stages. Sem cache injetado, simplesmente não cacheia
        # (evita I/O em disco surpresa para quem instancia isoladamente,
        # como os testes unitários).
        self.cache = cache

    async def generate_perspectives_with_queries(
        self, topic: str, num_perspectives: int = 3
    ) -> List[Dict[str, Any]]:
        """Gera perfis de especialistas e suas respectivas sub-queries em uma chamada estruturada.

        Retorna:
            Lista de dicionários contendo:
            - 'name': Nome da persona/especialista
            - 'description': Foco de análise da persona
            - 'sub_queries': Lista de sub-queries para busca
        """
        topic = (topic or "").strip()
        if not topic:
            logger.warning(
                "storm_perspectives: tópico vazio recebido; abortando para fallback genérico."
            )
            return self._fallback("general topic", num_perspectives)

        num_perspectives = max(
            _MIN_PERSPECTIVES, min(num_perspectives, _MAX_PERSPECTIVES)
        )

        logger.info(
            f"Gerando {num_perspectives} perspectivas STORM para o tópico: '{topic[:50]}'"
        )

        cache_key = self._cache_key(topic, num_perspectives)
        if self.cache is not None:
            try:
                cached = await self.cache.get(cache_key)
            except Exception as e:
                logger.warning(f"storm_perspectives: falha ao consultar cache: {e}")
                cached = None
            if cached:
                logger.info("storm_perspectives: perspectivas recuperadas do cache.")
                return cached

        prompt = (
            "You are a research director setting up a panel of diverse experts to investigate a topic.\n\n"
            f"Topic: {topic}\n\n"
            f"Generate exactly {num_perspectives} distinct expert perspectives (stakeholders, specialists, "
            "or critics) that examine this topic from complementary, non-overlapping viewpoints.\n"
            "For each expert, provide:\n"
            "1. Name: The expert's title (e.g., 'Lead Security Auditor', 'Business Operations Manager').\n"
            "2. Description: A short sentence detailing their unique angle or concerns regarding the topic.\n"
            "3. Sub-queries: Exactly 2 highly specific, search-engine-ready keywords/queries they would run to gather data for their angle.\n\n"
            "Return ONLY a valid JSON array of objects with the keys 'name', 'description', and 'sub_queries' (which is an array of strings)."
        )

        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "sub_queries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "sub_queries"],
            },
        }

        try:
            await self._track_llm_call(prompt)
            raw_perspectives = await self.llm.generate_structured(
                prompt, schema, temperature=0.5
            )
            validated = self._validate(raw_perspectives, num_perspectives)
            if validated:
                if self.cache is not None:
                    try:
                        await self.cache.set(
                            cache_key,
                            validated,
                            ttl_seconds=_CACHE_TTL_SECONDS,
                            source_type="storm_perspectives",
                        )
                    except Exception as e:
                        logger.warning(
                            f"storm_perspectives: falha ao gravar cache: {e}"
                        )
                return validated
            logger.warning(
                "storm_perspectives: LLM retornou dados sem nenhuma perspectiva válida; usando fallback."
            )
        except Exception as e:
            logger.warning(f"Falha na geração de perspectivas STORM via LLM: {e}")

        return self._fallback(topic, num_perspectives)

    # ── Auxiliares ───────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(topic: str, num_perspectives: int) -> str:
        fingerprint = f"{topic.strip().lower()}|{num_perspectives}"
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"storm_perspectives:{digest}"

    async def _track_llm_call(self, prompt: str) -> None:
        """Registra tokens/custo estimado da chamada no `TokenEconomy`, se disponível.

        Antes este bloco fazia `hasattr(...) and isinstance(...)` e não fazia
        nada com o resultado (no-op morto). Agora efetivamente conta tokens e
        estima custo, replicando o padrão usado em `DeepResearcher._track_llm_call`.
        """
        if hasattr(self.llm, "token_economy") and isinstance(
            self.llm.token_economy, TokenEconomy
        ):
            try:
                tokens = self.llm.token_economy.count_tokens(prompt)
                _, cost = self.llm.token_economy.estimate_cost(
                    prompt, output_tokens=400
                )
                logger.debug(
                    f"storm_perspectives: chamada LLM ~{tokens} tokens, custo estimado ${cost:.5f}"
                )
            except Exception as e:
                logger.debug(f"storm_perspectives: falha ao contabilizar custo: {e}")

    @staticmethod
    def _validate(raw_perspectives: Any, num_perspectives: int) -> List[Dict[str, Any]]:
        """Valida e normaliza a resposta bruta do LLM.

        Melhorias em relação à versão anterior:
        - Descarta personas com nome vazio ou duplicado (LLMs às vezes repetem
          a mesma persona com fraseados levemente diferentes).
        - Descarta/normaliza sub_queries vazias ou só com espaços.
        - Descarta personas sem nenhuma sub_query utilizável (antes uma
          persona com `sub_queries: []` passava e quebrava consumidores que
          esperam pelo menos uma query por especialista).
        - Corta o resultado em `num_perspectives`, mesmo quando o LLM ignora
          a instrução e devolve mais personas do que o pedido — o fallback já
          fazia isso, a validação não.
        """
        if not isinstance(raw_perspectives, list):
            return []

        validated: List[Dict[str, Any]] = []
        seen_names: set[str] = set()

        for p in raw_perspectives:
            if not isinstance(p, dict) or "name" not in p or "sub_queries" not in p:
                continue

            name = str(p["name"]).strip()
            if not name or name.lower() in seen_names:
                continue

            raw_sub_queries = p.get("sub_queries", [])
            if not isinstance(raw_sub_queries, list):
                continue
            sub_queries = [str(sq).strip() for sq in raw_sub_queries if str(sq).strip()]
            if not sub_queries:
                continue

            seen_names.add(name.lower())
            validated.append(
                {
                    "name": name,
                    "description": str(p.get("description", "")).strip(),
                    "sub_queries": sub_queries,
                }
            )

        return validated[:num_perspectives]

    @staticmethod
    def _fallback(topic: str, num_perspectives: int) -> List[Dict[str, Any]]:
        """Fallback resiliente caso o LLM falhe, retorne vazio ou lixo."""
        return [
            {
                "name": "Technical Architect",
                "description": "Focuses on technology stacks, reliability, and code implementations.",
                "sub_queries": [
                    f"{topic} technical architecture",
                    f"{topic} code implementation examples",
                ],
            },
            {
                "name": "Security & Compliance Auditor",
                "description": "Focuses on threat modeling, security vulnerabilities, and licensing issues.",
                "sub_queries": [
                    f"{topic} security vulnerabilities",
                    f"{topic} licensing compliance",
                ],
            },
            {
                "name": "Product & Business Strategist",
                "description": "Focuses on market trends, cost optimization, and user experience.",
                "sub_queries": [
                    f"{topic} business cost impact",
                    f"{topic} market adoption trends",
                ],
            },
        ][:num_perspectives]
