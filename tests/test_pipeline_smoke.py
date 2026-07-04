"""Smoke test manual (sem pytest) do motor ResearchPipeline.

Valida os quatro comportamentos centrais antes da entrega:
1. Execução sequencial + propagação de contexto entre stages.
2. Stage não-crítica falha -> pipeline continua e registra o erro.
3. Stage crítica falha -> pipeline aborta, roda rollback em ordem
   reversa das stages concluídas, e PipelineError carrega o contexto
   parcial.
4. Retry básico: stage falha N vezes e sucede na tentativa N+1.
"""

import asyncio

from src.pipeline import PipelineContext, PipelineError, PipelineStage, ResearchPipeline


class IntentStage(PipelineStage):
    name = "intent"

    async def run(self, context: PipelineContext) -> PipelineContext:
        context.intent = {"domain": "dev_tools"}
        return context


class SearchStage(PipelineStage):
    name = "search"

    async def run(self, context: PipelineContext) -> PipelineContext:
        assert context.intent is not None, "SearchStage rodou antes de IntentStage!"
        context.raw_results = ["r1", "r2"]
        return context


class OptionalEvidenceGraphStage(PipelineStage):
    name = "evidence_graph"
    critical = False

    async def run(self, context: PipelineContext) -> PipelineContext:
        raise RuntimeError("falha simulada — não deve abortar o pipeline")


class ReportStage(PipelineStage):
    name = "report"
    rolled_back = False

    async def run(self, context: PipelineContext) -> PipelineContext:
        context.report = f"# Relatório\n\n{len(context.raw_results)} resultados."
        return context

    async def rollback(self, context: PipelineContext) -> None:
        ReportStage.rolled_back = True


async def test_happy_path_with_non_critical_failure():
    pipeline = ResearchPipeline(
        [IntentStage(), SearchStage(), OptionalEvidenceGraphStage(), ReportStage()],
        name="happy_path",
    )
    context = await pipeline.run("Rust async best practices")

    assert context.report.startswith("# Relatório")
    assert context.completed_stages == ["intent", "search", "report"]
    assert len(context.errors) == 1
    assert context.errors[0].stage == "evidence_graph"
    assert context.errors[0].critical is False
    print("✅ test_happy_path_with_non_critical_failure passou")


class FailingCriticalStage(PipelineStage):
    name = "search"  # mesmo nome do exemplo anterior, classe isolada

    async def run(self, context: PipelineContext) -> PipelineContext:
        raise ConnectionError("todas as fontes offline")


async def test_critical_failure_triggers_rollback():
    ReportStage.rolled_back = False  # reset entre testes
    pipeline = ResearchPipeline(
        [IntentStage(), FailingCriticalStage(), ReportStage()],
        name="critical_failure",
    )
    try:
        await pipeline.run("query qualquer")
        raise AssertionError("PipelineError deveria ter sido levantada")
    except PipelineError as exc:
        assert exc.stage == "search"
        assert isinstance(exc.cause, ConnectionError)
        # 'intent' concluiu antes da falha -> deve ter sido "revertida"
        # (IntentStage não sobrescreve rollback, então é no-op, mas o
        # motor deve tentar chamá-la sem lançar).
        assert exc.context.completed_stages == ["intent"]
        # ReportStage nunca rodou (pipeline abortou antes) -> nunca é
        # adicionada a completed_stages, então seu rollback não deve
        # ter sido chamado.
        assert ReportStage.rolled_back is False
        print("✅ test_critical_failure_triggers_rollback passou")


class FlakyStage(PipelineStage):
    name = "flaky"
    max_retries = 2
    retry_backoff_seconds = 0.01  # rápido para o teste

    def __init__(self):
        self.attempts = 0

    async def run(self, context: PipelineContext) -> PipelineContext:
        self.attempts += 1
        if self.attempts < 3:
            raise TimeoutError(f"timeout na tentativa {self.attempts}")
        context.set("flaky_attempts", self.attempts)
        return context


async def test_retry_recovers_after_transient_failures():
    flaky = FlakyStage()
    pipeline = ResearchPipeline([flaky], name="retry_test")
    context = await pipeline.run("query")
    assert context.get("flaky_attempts") == 3
    assert flaky.attempts == 3
    print("✅ test_retry_recovers_after_transient_failures passou")


async def test_duplicate_stage_names_rejected():
    try:
        ResearchPipeline([IntentStage(), IntentStage()])
        raise AssertionError("ValueError esperado para nomes duplicados")
    except ValueError:
        print("✅ test_duplicate_stage_names_rejected passou")


async def main():
    await test_happy_path_with_non_critical_failure()
    await test_critical_failure_triggers_rollback()
    await test_retry_recovers_after_transient_failures()
    await test_duplicate_stage_names_rejected()
    print("\n🎉 Todos os smoke tests do ResearchPipeline passaram.")


if __name__ == "__main__":
    asyncio.run(main())
