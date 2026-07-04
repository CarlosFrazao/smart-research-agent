"""Smoke test manual (sem pytest) da IntentStage.

Diferente do smoke test genérico de `pipeline.py`, este usa o
`IntentAnalyzer` REAL (não um stub) em queries cujo domínio/intenção
são detectados heuristicamente com confiança — o que evita precisar de
um LLMClient de verdade (curto-circuito interno do IntentAnalyzer, ver
docstring de `intent_stage.py`). Isso valida a integração de fato, não
apenas o contrato mockado.
"""

import asyncio

from src.intent_analyzer import IntentAnalyzer
from src.pipeline import PipelineError, ResearchPipeline
from src.pipeline.stages import IntentStage
from src.types import Domain, Intention


async def test_intent_stage_real_heuristic_path():
    # llm_client=None é seguro aqui: a query abaixo é resolvida pelo
    # curto-circuito heurístico do IntentAnalyzer (domínio + intenção
    # claros), então o LLM nunca é chamado.
    analyzer = IntentAnalyzer(llm_client=None)
    stage = IntentStage(analyzer)

    pipeline = ResearchPipeline([stage], name="intent_only")
    context = await pipeline.run("Docker vs Kubernetes para deploy self-hosted")

    assert context.intent is not None
    assert context.intent.domain == Domain.INFRASTRUCTURE
    assert context.intent.intention == Intention.COMPARE
    assert context.get("domain") == "infrastructure"
    assert context.get("intention") == "compare"
    assert context.completed_stages == ["intent"]
    print("✅ test_intent_stage_real_heuristic_path passou")


class BrokenIntentAnalyzer:
    """Simula um bug real (não uma falha transiente de LLM) para provar
    que IntentStage.critical=True aborta o pipeline corretamente."""

    async def analyze(self, query: str, force_llm: bool = False):
        raise TypeError("bug simulado: domain inválido no enum")


async def test_intent_stage_unexpected_failure_aborts_pipeline():
    stage = IntentStage(BrokenIntentAnalyzer())
    pipeline = ResearchPipeline([stage], name="intent_broken")

    try:
        await pipeline.run("qualquer query")
        raise AssertionError("PipelineError esperado")
    except PipelineError as exc:
        assert exc.stage == "intent"
        assert isinstance(exc.cause, TypeError)
        assert exc.context.intent is None  # nunca chegou a ser setado
        print("✅ test_intent_stage_unexpected_failure_aborts_pipeline passou")


async def test_intent_stage_uses_enriched_query_when_present():
    captured = {}

    class SpyAnalyzer:
        async def analyze(self, query: str, force_llm: bool = False):
            captured["query"] = query
            from src.types import IntentResult

            return IntentResult(
                domain=Domain.GENERAL,
                entities=[],
                intention=Intention.DISCOVER,
                urgency="nao",
                confidence="media",
            )

    stage = IntentStage(SpyAnalyzer())
    pipeline = ResearchPipeline([stage], name="intent_enriched")
    await pipeline.run(
        "query original",
        enriched_query="contexto de memória\n\n---\n\nQuery atual: query original",
    )

    assert captured["query"].startswith("contexto de memória")
    print("✅ test_intent_stage_uses_enriched_query_when_present passou")


async def main():
    await test_intent_stage_real_heuristic_path()
    await test_intent_stage_unexpected_failure_aborts_pipeline()
    await test_intent_stage_uses_enriched_query_when_present()
    print("\n🎉 Todos os smoke tests da IntentStage passaram.")


if __name__ == "__main__":
    asyncio.run(main())
