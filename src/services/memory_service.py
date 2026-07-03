import logging

logger = logging.getLogger("orchestrator.memory_service")


class MemoryService:
    """
    Interface simplificada para interagir com a OrvixMemoryV2 (RAG Híbrido e Grafo de Conhecimento).
    """

    def __init__(self, orchestrator):
        self.orch = orchestrator

    @property
    def memory(self):
        return self.orch.memory

    def get_context(self, query: str) -> str:
        """
        Recupera contexto de pesquisas passadas.
        """
        if not self.memory:
            return ""
        try:
            return self.memory.get_context(query, top_k=3)
        except Exception as e:
            logger.warning(f"MemoryService: get_context falhou: {e}")
            return ""

    def store(self, query: str, executive_summary: str, top_entities: list[str], domain: str, duration_seconds: float) -> None:
        """
        Salva o resultado de pesquisa atual na memória de longo prazo de forma assíncrona/segura.
        """
        if not self.memory:
            return
        try:
            self.memory.store_research_result(
                query=query,
                executive_summary=executive_summary,
                top_entities=top_entities,
                domain=domain,
                duration_seconds=duration_seconds,
            )
            logger.info("MemoryService: resultado de pesquisa armazenado com sucesso.")
        except Exception as e:
            logger.warning(f"MemoryService: store_research_result falhou: {e}")
