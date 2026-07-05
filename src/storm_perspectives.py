"""Módulo STORM Perspectives Generator.

Simula múltiplos especialistas/personas (STORM style) para gerar
direcionamentos de busca a partir de visões complementares e profundas.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from src.clients.llm_client import LLMClient
from src.token_economy import TokenEconomy

logger = logging.getLogger("storm-perspectives")


class StormPerspectiveGenerator:
    """Gera personas especialistas (perspectivas) e sub-queries direcionadas para um tópico."""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

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
        logger.info(
            f"Gerando {num_perspectives} perspectivas STORM para o tópico: '{topic[:50]}'"
        )

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

        # Registra custos estimados se a token_economy estiver vinculada
        if hasattr(self.llm, "token_economy") and isinstance(
            self.llm.token_economy, TokenEconomy
        ):
            # Apenas para registro no budget da sessão geral
            pass

        try:
            raw_perspectives = await self.llm.generate_structured(
                prompt, schema, temperature=0.5
            )
            if isinstance(raw_perspectives, list):
                # Validação rápida de integridade estrutural
                validated = []
                for p in raw_perspectives:
                    if isinstance(p, dict) and "name" in p and "sub_queries" in p:
                        validated.append(
                            {
                                "name": str(p["name"]),
                                "description": str(p.get("description", "")),
                                "sub_queries": [str(sq) for sq in p["sub_queries"]],
                            }
                        )
                if validated:
                    return validated
        except Exception as e:
            logger.warning(f"Falha na geração de perspectivas STORM via LLM: {e}")

        # Fallback resiliente caso o LLM falhe
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
