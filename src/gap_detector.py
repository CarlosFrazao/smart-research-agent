from typing import List
from src.types import RankedResult, GapAnalysis, IntentResult
from src.clients.llm_client import LLMClient
import logging

logger = logging.getLogger(__name__)


class GapDetector:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.max_iterations = 3

    async def detect(
        self, results: List[RankedResult], query: str, intent: IntentResult
    ) -> GapAnalysis:
        source_coverage = len(set(r.source for r in results))
        top_projects = len(set(self._extract_project(r.title) for r in results[:20]))

        if len(results) < 10:
            return GapAnalysis(
                is_complete=False,
                missing_aspects=["poucos resultados"],
                new_queries=[f"{query} open source", f"{query} alternative"],
                confidence="alta",
                rationale="Menos de 10 resultados encontrados",
            )

        if source_coverage < 3:
            return GapAnalysis(
                is_complete=False,
                missing_aspects=["cobertura de fontes insuficiente"],
                new_queries=[f"{query} site:github.com", f"{query} reddit"],
                confidence="media",
                rationale=f"Apenas {source_coverage} fontes cobertas",
            )

        if top_projects < 3:
            return GapAnalysis(
                is_complete=False,
                missing_aspects=["pouca diversidade de projetos"],
                new_queries=[f"best {query} 2026", f"{query} vs"],
                confidence="media",
                rationale="Menos de 3 projetos distintos nos top 20",
            )

        prompt_text = (
            "Voce e um auditor de qualidade de pesquisa tecnica.\n"
            "Analise os resultados e identifique lacunas.\n\n"
            "Criterios:\n"
            "1. Cobertura: principais fontes foram pesquisadas?\n"
            "2. Diversidade: resultados de projetos diferentes?\n"
            "3. Atualidade: resultados recentes (ultimos 12 meses)?\n"
            "4. Profundidade: ha analises comparativas, reviews?\n"
            "5. Conflitos: opinioes divergentes?\n\n"
            f"Query: {query}\n"
            f"Fontes cobertas: {source_coverage}\n"
            f"Projetos distintos (top 20): {top_projects}\n"
            f"Total resultados: {len(results)}\n\n"
            "Top 10 resultados:\n"
        )
        for i, r in enumerate(results[:10]):
            prompt_text += f"{i+1}. [{r.source}] {r.title} (score: {r.score})\n"

        prompt_text += (
            "\nResponda em JSON:\n"
            "{\n"
            '  "is_complete": true,\n'
            '  "missing_aspects": ["string"],\n'
            '  "new_queries": ["string"],\n'
            '  "confidence": "alta|media|baixa",\n'
            '  "rationale": "string"\n'
            "}\n"
        )

        schema = {
            "type": "object",
            "properties": {
                "is_complete": {"type": "boolean"},
                "missing_aspects": {"type": "array", "items": {"type": "string"}},
                "new_queries": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["is_complete", "missing_aspects", "new_queries", "confidence", "rationale"],
        }

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
            return GapAnalysis(**result)
        except Exception as e:
            logger.warning(f"LLM gap detection falhou: {e}")
            return GapAnalysis(
                is_complete=True,
                missing_aspects=[],
                new_queries=[],
                confidence="media",
                rationale="Heuristicas indicam pesquisa suficiente",
            )

    def _extract_project(self, title: str) -> str:
        if "/" in title:
            return title.split("/")[-1]
        return title.split()[0] if title else ""
