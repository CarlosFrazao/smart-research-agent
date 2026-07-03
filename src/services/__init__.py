"""
src/services — Pacote de Serviços Especializados do SRA v6.

Cada serviço encapsula uma responsabilidade do Orchestrator:
- SearchService:    execução paralela de buscas
- ReasoningService: intent, expand, rank, gap, sanitize, synthesize
- MemoryService:    OrvixMemoryV2 (get_context + store)
- ReportService:    geração, score, peer review, obsidian sync
"""
