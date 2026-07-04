"""src/pipeline/stages/intent_stage.py — Stage de análise de intenção.

Primeira stage do `ResearchPipeline` (ver `src/pipeline/pipeline.py`,
item 21). Classifica domínio, intenção, urgência e entidades da query
delegando ao `IntentAnalyzer` já existente — esta stage é um adaptador
fino entre o motor genérico do pipeline e a lógica de domínio, não
reimplementa nenhuma heurística.

Por que é `critical = True`
----------------------------
`SourcePlanner` (fontes a consultar), `QueryExpander` (variações de
query) e o roteamento por domínio no `RankStage` dependem de
`context.intent.domain`/`.intention`. Sem essa stage, o resto do
pipeline não tem para onde ir — diferente de stages best-effort como
EvidenceGraph ou PeerReview (ver item 26-29), que enriquecem o relatório
mas não bloqueiam o fluxo se falharem.

Por que não há retry configurado aqui
--------------------------------------
`IntentAnalyzer.analyze()` já é resiliente por construção: primeiro
tenta um curto-circuito heurístico (custo zero, sem chamada de rede) e,
quando precisa do LLM, envolve a chamada em `try/except` internamente,
caindo de volta para o resultado heurístico em caso de falha (rate
limit, timeout, JSON malformado). Uma exceção escapando até esta stage
indica um erro genuinamente inesperado (bug, `TypeError` em enum
inválido, etc.) — nesse caso, `max_retries` extra na própria stage não
ajudaria; deixamos `critical=True` abortar o pipeline gerar o rollback
e o `PipelineError` correto para diagnóstico, em vez de mascarar o
problema retentando.
"""

from __future__ import annotations

from src.intent_analyzer import IntentAnalyzer
from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.utils.logging import setup_logger

logger = setup_logger("pipeline.intent_stage")

__all__ = ["IntentStage"]


class IntentStage(PipelineStage):
    """Classifica domínio, intenção, urgência e entidades da query.

    Popula:
        - ``context.intent``: o `IntentResult` completo (`src/types.py`),
          consumido por stages downstream (`ExpandStage`, `SearchStage`
          via `SourcePlanner`, `RankStage`).
        - ``context.extra["domain"]`` / ``context.extra["intention"]``:
          atalhos em string (via `.get("domain")`) para stages ou
          código de logging/telemetria que não precisam do objeto
          tipado completo.
    """

    name = "intent"
    critical = True

    def __init__(self, intent_analyzer: IntentAnalyzer, *, force_llm: bool = False):
        """
        Args:
            intent_analyzer: Instância já configurada de `IntentAnalyzer`
                (injetada pela `StageFactory` — item 40 — a partir do
                `LLMClient` compartilhado do container de dependências).
            force_llm: Repassado para `IntentAnalyzer.analyze()`. Força
                a chamada LLM mesmo quando a heurística já é confiante.
                Útil em testes/benchmarks de qualidade; em produção o
                default `False` é o que economiza tokens.
        """
        self.intent_analyzer = intent_analyzer
        self.force_llm = force_llm

    async def run(self, context: PipelineContext) -> PipelineContext:
        query = context.enriched_query or context.query
        logger.info(f"IntentStage: analisando intenção — '{query[:80]}'")

        intent = await self.intent_analyzer.analyze(query, force_llm=self.force_llm)

        context.intent = intent
        context.set("domain", intent.domain.value)
        context.set("intention", intent.intention.value)

        logger.info(
            f"IntentStage: domínio={intent.domain.value}, "
            f"intenção={intent.intention.value}, urgência={intent.urgency}, "
            f"confiança={intent.confidence}, entidades={intent.entities}"
        )
        return context
