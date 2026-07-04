"""Smoke test manual (e via pytest) do SearchStage.

Testa os comportamentos centrais do SearchStage:
  1. Execução paralela de buscas e cache.
  2. Controle de concorrência por fonte (semáforos).
  3. Circuit breaker por fonte (abrindo após falhas consecutivas).
  4. Early termination quando resultados de alta qualidade são atingidos.
"""

import asyncio
import pytest
from datetime import datetime

from src.pipeline.pipeline import PipelineContext, PipelineError
from src.pipeline.stages.search_stage import SearchStage, SearchStageConfig
from src.types import Domain, Intention, IntentResult, ExpandedQuery, SourcePlan, SearchResult, RankedResult
from src.utils.circuit_breaker import CircuitBreakerRegistry


# ── Mocks para Teste ────────────────────────────────────────────────────────

class MockSearcher:
    def __init__(self, name: str, results: list[SearchResult], delay: float = 0.0, fail: bool = False):
        self.name = name
        self.results = results
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.enabled = True

    async def search(self, query: str, domain: str | None = None) -> list[SearchResult]:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"Searcher {self.name} falhou intencionalmente")
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.results


class MockCache:
    def __init__(self):
        self.store = {}
        self.gets = 0
        self.sets = 0

    async def get(self, namespace: str, key: str) -> list[dict] | None:
        self.gets += 1
        return self.store.get(f"{namespace}:{key}")

    async def set(self, namespace: str, key: str, value: list[dict], ttl: int | None = None) -> None:
        self.sets += 1
        self.store[f"{namespace}:{key}"] = value


class MockRanker:
    async def rank(self, results: list[SearchResult]) -> list[RankedResult]:
        # Apenas transforma SearchResult em RankedResult preservando dados
        ranked_list = []
        for r in results:
            # Se for do mock, calcula um score mockado com base no título
            score = 90.0 if "good" in r.title.lower() else 50.0
            ranked_list.append(
                RankedResult(
                    source=r.source,
                    title=r.title,
                    url=r.url,
                    description=r.description,
                    metrics=r.metrics,
                    raw=r.raw,
                    fetched_at=r.fetched_at,
                    confidence_score=r.confidence_score,
                    evidence_quality=r.evidence_quality,
                    citations=r.citations,
                    contradictions=r.contradictions,
                    hallucination_flags=r.hallucination_flags,
                    score=score,
                )
            )
        return ranked_list


# ── Testes ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_stage_happy_path():
    CircuitBreakerRegistry.reset_all()

    # Cria os mocks
    res1 = SearchResult(source="mock1", title="good result", url="https://mock1.com", description="desc")
    s1 = MockSearcher("mock1", [res1])
    searchers = {"mock1": s1}
    cache = MockCache()
    ranker = MockRanker()

    stage = SearchStage(searchers, cache, ranker)

    # Contexto
    intent = IntentResult(
        domain=Domain.DEV_TOOLS,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta"
    )
    expanded = [ExpandedQuery(query="test query", type="original")]
    source_plan = SourcePlan(
        sources={"mock1": expanded},
        primary=["mock1"],
        secondary=[]
    )

    context = PipelineContext(query="test query")
    context.intent = intent
    context.expanded_queries = expanded
    context.source_plan = source_plan

    # Executa o stage diretamente
    res_context = await stage.run(context)

    assert len(res_context.raw_results) == 1
    assert res_context.raw_results[0].title == "good result"
    assert len(res_context.ranked_results) == 1
    assert res_context.ranked_results[0].score == 90.0
    assert s1.calls == 1
    assert cache.sets == 1  # salvou no cache


@pytest.mark.asyncio
async def test_search_stage_circuit_breaker():
    CircuitBreakerRegistry.reset_all()

    # Searcher configurado para falhar
    s1 = MockSearcher("mock1", [], fail=True)
    searchers = {"mock1": s1}
    cache = MockCache()
    ranker = MockRanker()

    # Registra com threshold de 2 falhas para o teste
    config = SearchStageConfig(
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_timeout=1.0,
    )
    stage = SearchStage(searchers, cache, ranker, config=config)

    intent = IntentResult(
        domain=Domain.DEV_TOOLS,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta"
    )
    expanded = [ExpandedQuery(query="query_valida", type="original")]
    source_plan = SourcePlan(sources={"mock1": expanded})

    # Força 3 execuções. A partir da 3ª, o Circuit Breaker deve abrir
    # e não deve chamar o searcher.search() novamente.
    for i in range(3):
        context = PipelineContext(query="query_valida")
        context.intent = intent
        context.expanded_queries = expanded
        context.source_plan = source_plan
        await stage.run(context)

    # O searcher deve ter sido chamado exatamente 2 vezes (na 3ª o circuito já estava OPEN)
    assert s1.calls == 2


@pytest.mark.asyncio
async def test_search_stage_early_termination():
    CircuitBreakerRegistry.reset_all()

    # Dois searchers. mock1 retorna resultado de alta qualidade rapidamente.
    # mock2 é muito lento.
    res_high = SearchResult(source="mock1", title="good result", url="https://mock1.com", description="desc", confidence_score=0.90)
    s1 = MockSearcher("mock1", [res_high])
    s2 = MockSearcher("mock2", [], delay=10.0) # muito lento
    searchers = {"mock1": s1, "mock2": s2}
    cache = MockCache()
    ranker = MockRanker()

    config = SearchStageConfig(
        early_termination_enabled=True,
        early_termination_threshold=0.85,
        early_termination_count=1,
    )
    stage = SearchStage(searchers, cache, ranker, config=config)

    intent = IntentResult(
        domain=Domain.DEV_TOOLS,
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta"
    )
    expanded_1 = [ExpandedQuery(query="query_um", type="original")]
    expanded_2 = [ExpandedQuery(query="query_dois", type="original")]
    source_plan = SourcePlan(sources={"mock1": expanded_1, "mock2": expanded_2})

    context = PipelineContext(query="query_original")
    context.intent = intent
    context.expanded_queries = expanded_1 + expanded_2
    context.source_plan = source_plan

    # O pipeline deve terminar quase instantaneamente e não esperar os 10s do s2
    start_time = asyncio.get_event_loop().time()
    await stage.run(context)
    duration = asyncio.get_event_loop().time() - start_time

    assert duration < 2.0
    assert s1.calls == 1


async def run_tests_manual():
    print("Iniciando testes manuais de SearchStage...")
    await test_search_stage_happy_path()
    await test_search_stage_circuit_breaker()
    await test_search_stage_early_termination()
    print("🎉 Todos os testes de SearchStage passaram!")


if __name__ == "__main__":
    asyncio.run(run_tests_manual())
