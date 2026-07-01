import re
from typing import List
from src.types import IntentResult, Domain, Intention
from src.clients.llm_client import LLMClient
import logging

logger = logging.getLogger(__name__)

DOMAIN_KEYWORDS = {
    Domain.SAAS_B2B: ["crm", "erp", "helpdesk", "marketing", "saas", "b2b", "sales", "support"],
    Domain.DEV_TOOLS: ["ide", "linter", "ci/cd", "testing", "debugger", "git", "vscode", "editor"],
    Domain.AI_ML: ["llm", "model", "ai", "ml", "neural", "transformer", "gpt", "claude", "embedding"],
    Domain.AUTOMATION: ["n8n", "zapier", "make", "rpa", "workflow", "automation", "pipeline"],
    Domain.INFRASTRUCTURE: ["docker", "kubernetes", "k8s", "cloud", "serverless", "terraform", "aws"],
    Domain.OPEN_SOURCE: ["github", "open source", "library", "framework", "package", "npm", "pypi"],
}

INTENTION_KEYWORDS = {
    Intention.COMPARE: ["compare", "vs", "versus", "better than", "alternative to", "difference"],
    Intention.LEARN: ["how does", "what is", "how to", "tutorial", "explain", "guide"],
    Intention.IMPLEMENT: ["install", "setup", "deploy", "configure", "self-host", "docker run"],
    Intention.EVALUATE: ["worth it", "pros and cons", "review", "should i use", "good"],
}


class IntentAnalyzer:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def _heuristic_domain(self, query: str) -> Domain:
        query_lower = query.lower()
        scores = {domain: 0 for domain in Domain}
        for domain, keywords in DOMAIN_KEYWORDS.items():
            for kw in keywords:
                if kw in query_lower:
                    scores[domain] += 1
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else Domain.GENERAL

    def _heuristic_intention(self, query: str) -> Intention:
        query_lower = query.lower()
        for intention, keywords in INTENTION_KEYWORDS.items():
            for kw in keywords:
                if kw in query_lower:
                    return intention
        return Intention.DISCOVER

    def _heuristic_urgency(self, query: str) -> str:
        urgent = ["2026", "2025", "new", "latest", "trending", "recent", "now", "this year"]
        return "sim" if any(u in query.lower() for u in urgent) else "nao"

    def _extract_entities_heuristic(self, query: str) -> List[str]:
        entities = re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", query)
        repos = re.findall(r"\b[\w-]+/[\w-]+\b", query)
        return list(set(entities + repos))

    async def analyze(self, query: str) -> IntentResult:
        domain = self._heuristic_domain(query)
        intention = self._heuristic_intention(query)
        urgency = self._heuristic_urgency(query)
        entities = self._extract_entities_heuristic(query)

        prompt_text = (
            "Voce e um analisador de intencao especializado em tecnologia.\n"
            "Analise a query e classifique em JSON:\n\n"
            f"Query: {query}\n"
            f"Heuristica inicial: dominio={domain.value}, intencao={intention.value}, urgencia={urgency}\n\n"
            "Responda em JSON valido:\n"
            '{\n'
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
            return IntentResult(
                domain=Domain(result.get("domain", domain.value)),
                entities=result.get("entities", entities),
                intention=Intention(result.get("intention", intention.value)),
                urgency=result.get("urgency", urgency),
                confidence=result.get("confidence", "media"),
            )
        except Exception as e:
            logger.warning(f"LLM intent analysis falhou, usando heuristica: {e}")
            return IntentResult(
                domain=domain,
                entities=entities,
                intention=intention,
                urgency=urgency,
                confidence="media",
            )
