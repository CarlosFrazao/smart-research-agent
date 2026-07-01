from typing import List
from src.types import ExpandedQuery, IntentResult
from src.clients.llm_client import LLMClient
import logging

logger = logging.getLogger(__name__)


class QueryExpander:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    async def expand(self, query: str, intent: IntentResult) -> List[ExpandedQuery]:
        prompt_text = (
            "Você é um especialista em expansão de queries de pesquisa.\n"
            "Gere variações inteligentes que maximizam a cobertura de informação.\n\n"
            "Estratégias a aplicar:\n"
            "1. Variações de terminologia (sinônimos técnicos, abreviações)\n"
            "2. Perspectivas diferentes (implementação, comparação, crítica, casos de uso)\n"
            "3. Queries de evidência (benchmarks, reviews, dados reais)\n"
            "4. Queries de comunidade (Reddit, HN)\n\n"
            "Regras:\n"
            "- Gere entre 8 e 12 queries (nem menos, nem mais)\n"
            "- NÃO gere queries que retornariam os mesmos resultados da query original\n"
            "- Prioridade alta = pesquisar primeiro, baixa = pesquisar por último\n"
            "- Use inglês para termos técnicos (melhor cobertura no GitHub/HN)\n\n"
            f"Query original: {query}\n"
            f"Dominio: {intent.domain.value}\n"
            f"Entidades: {', '.join(intent.entities)}\n\n"
            'Responda em JSON válido:\n'
            '{\n'
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

        schema = {
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

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
            queries = result.get("queries", [])
            return [ExpandedQuery(**q) for q in queries]
        except Exception as e:
            logger.warning(f"LLM query expansion falhou, usando fallback: {e}")
            return self._fallback_expand(query, intent)

    def _fallback_expand(self, query: str, intent: IntentResult) -> List[ExpandedQuery]:
        base = query.lower()
        expansions = [
            ExpandedQuery(query=base, type="original", priority="alta", rationale="query original"),
            ExpandedQuery(query=f"open source {base}", type="qualificador", priority="alta", rationale="encontra projetos open source"),
            ExpandedQuery(query=f"self hosted {base}", type="qualificador", priority="alta", rationale="encontra alternativas self-hosted"),
            ExpandedQuery(query=f"best {base} 2026", type="temporal", priority="media", rationale="resultados recentes"),
            ExpandedQuery(query=f"{base} alternative", type="comparacao", priority="media", rationale="encontra alternativas"),
            ExpandedQuery(query=f"github {base}", type="plataforma", priority="media", rationale="busca direta no GitHub"),
        ]
        for entity in intent.entities[:2]:
            expansions.append(
                ExpandedQuery(
                    query=f"alternative to {entity}",
                    type="comparacao",
                    priority="media",
                    rationale=f"comparacao direta com {entity}",
                )
            )
        return expansions[:12]
