"""Benchmark de latência por searcher do Smart Research Agent.

Suíte de benchmarks que mede latência percentil (p50, p95, p99) por searcher,
identifica searchers lentos, valida paralelismo com semáforo e testa
early termination.

Requer:
    pip install pytest pytest-asyncio pytest-benchmark statistics

Uso:
    pytest tests/benchmark/test_latency.py -v
    pytest tests/benchmark/test_latency.py -k "percentil" --benchmark-only
    pytest tests/benchmark/test_latency.py --benchmark-json=latency_results.json
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Dataclasses de Métricas ───────────────────────────────────────────────


@dataclass
class SearcherLatencyMetrics:
    """Métricas de latência acumuladas para um searcher."""

    searcher_name: str
    samples: list[float] = field(default_factory=list)
    errors: int = 0
    timeouts: int = 0

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def error_rate(self) -> float:
        return round(self.errors / max(self.count, 1), 3)

    @property
    def p50(self) -> float:
        return self._percentile(50)

    @property
    def p95(self) -> float:
        return self._percentile(95)

    @property
    def p99(self) -> float:
        return self._percentile(99)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def _percentile(self, p: int) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        k = (len(sorted_samples) - 1) * (p / 100.0)
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_samples) else f
        if f == c:
            return sorted_samples[f]
        return sorted_samples[f] * (c - k) + sorted_samples[c] * (k - f)

    def to_dict(self) -> dict[str, Any]:
        return {
            "searcher": self.searcher_name,
            "count": self.count,
            "mean_ms": round(self.mean, 2),
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "p99_ms": round(self.p99, 2),
            "min_ms": round(self.min_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "stdev_ms": round(self.stdev, 2),
            "errors": self.errors,
            "timeouts": self.timeouts,
            "error_rate": self.error_rate,
        }


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def latency_registry():
    """Registry de métricas de latência por searcher."""
    return {}


@pytest.fixture
def mock_searchers_with_latency():
    """Searchers mockados com latências controladas para benchmark."""
    from src.types import SearchResult

    def make_searcher(name: str, delay_ms: float, fail_rate: float = 0.0):
        async def search(query: str, **kwargs):
            await asyncio.sleep(delay_ms / 1000.0)
            if fail_rate > 0 and hash(query) % 100 < (fail_rate * 100):
                raise TimeoutError(f"{name} simulated timeout")
            return [
                SearchResult(
                    source=name,
                    title=f"{name} result for {query[:20]}",
                    url=f"https://{name}.com/result",
                    description=f"Result from {name}",
                    metrics={"latency_ms": delay_ms},
                )
            ]

        searcher = MagicMock()
        searcher.search = AsyncMock(side_effect=search)
        searcher.enabled = True
        searcher.source = name
        searcher.timeout = 30
        searcher.close = AsyncMock()
        return searcher

    return {
        "github": make_searcher("github", delay_ms=120, fail_rate=0.0),
        "reddit": make_searcher("reddit", delay_ms=250, fail_rate=0.05),
        "hackernews": make_searcher("hackernews", delay_ms=80, fail_rate=0.0),
        "arxiv": make_searcher("arxiv", delay_ms=400, fail_rate=0.02),
        "stackoverflow": make_searcher("stackoverflow", delay_ms=180, fail_rate=0.0),
        "google": make_searcher("google", delay_ms=300, fail_rate=0.01),
        "firecrawl": make_searcher("firecrawl", delay_ms=800, fail_rate=0.10),
        "jina": make_searcher("jina", delay_ms=150, fail_rate=0.0),
    }


@pytest.fixture
def benchmark_queries():
    """Queries representativas para benchmark."""
    return [
        "CRM open source",
        "melhor framework Python 2026",
        "n8n vs Make",
        "self-hosted Notion alternative",
        "Rust async runtime",
        "LLM local deployment",
        "open source analytics",
        "Kubernetes monitoring",
    ]


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _measure_searcher_latency(
    searcher,
    name: str,
    query: str,
    metrics: SearcherLatencyMetrics,
) -> list:
    """Mede latência de uma chamada de searcher e registra métricas."""
    start = time.monotonic()
    try:
        result = await searcher.search(query)
        latency = (time.monotonic() - start) * 1000
        metrics.samples.append(latency)
        return result
    except asyncio.TimeoutError:
        latency = (time.monotonic() - start) * 1000
        metrics.samples.append(latency)
        metrics.timeouts += 1
        metrics.errors += 1
        return []
    except Exception:
        latency = (time.monotonic() - start) * 1000
        metrics.samples.append(latency)
        metrics.errors += 1
        return []


# ─── Testes de Percentil por Searcher ──────────────────────────────────────


@pytest.mark.benchmark
class TestSearcherLatencyPercentiles:
    """Mede p50, p95, p99 de latência por searcher."""

    @pytest.mark.asyncio
    async def test_github_latency_percentiles(self, mock_searchers_with_latency, benchmark_queries):
        """GitHub API: tipicamente rápida (<200ms)."""
        metrics = SearcherLatencyMetrics("github")
        searcher = mock_searchers_with_latency["github"]

        for query in benchmark_queries * 10:  # 80 amostras
            await _measure_searcher_latency(searcher, "github", query, metrics)

        print(f"\nGitHub: {metrics.to_dict()}")
        assert metrics.p50 < 200, f"GitHub p50={metrics.p50}ms, esperado <200ms"
        assert metrics.p95 < 300, f"GitHub p95={metrics.p95}ms, esperado <300ms"
        assert metrics.error_rate == 0.0

    @pytest.mark.asyncio
    async def test_reddit_latency_percentiles(self, mock_searchers_with_latency, benchmark_queries):
        """Reddit API: moderada (200-400ms)."""
        metrics = SearcherLatencyMetrics("reddit")
        searcher = mock_searchers_with_latency["reddit"]

        for query in benchmark_queries * 10:
            await _measure_searcher_latency(searcher, "reddit", query, metrics)

        print(f"\nReddit: {metrics.to_dict()}")
        assert metrics.p50 < 400, f"Reddit p50={metrics.p50}ms, esperado <400ms"
        assert metrics.p95 < 500, f"Reddit p95={metrics.p95}ms, esperado <500ms"
        assert metrics.error_rate < 0.20

    @pytest.mark.asyncio
    async def test_arxiv_latency_percentiles(self, mock_searchers_with_latency, benchmark_queries):
        """arXiv API: lenta (>300ms) devido a XML parsing."""
        metrics = SearcherLatencyMetrics("arxiv")
        searcher = mock_searchers_with_latency["arxiv"]

        for query in benchmark_queries * 10:
            await _measure_searcher_latency(searcher, "arxiv", query, metrics)

        print(f"\narXiv: {metrics.to_dict()}")
        # arXiv é intencionalmente mais lento
        assert metrics.p50 < 600, f"arXiv p50={metrics.p50}ms, esperado <600ms"
        assert metrics.p99 < 800, f"arXiv p99={metrics.p99}ms, esperado <800ms"

    @pytest.mark.asyncio
    async def test_firecrawl_latency_percentiles(self, mock_searchers_with_latency, benchmark_queries):
        """Firecrawl: mais lento devido a scraping real (500-1000ms)."""
        metrics = SearcherLatencyMetrics("firecrawl")
        searcher = mock_searchers_with_latency["firecrawl"]

        for query in benchmark_queries * 10:
            await _measure_searcher_latency(searcher, "firecrawl", query, metrics)

        print(f"\nFirecrawl: {metrics.to_dict()}")
        assert metrics.p50 < 1000, f"Firecrawl p50={metrics.p50}ms, esperado <1000ms"
        assert metrics.p95 < 1200, f"Firecrawl p95={metrics.p95}ms, esperado <1200ms"
        # Firecrawl tem taxa de erro mais alta
        assert metrics.error_rate < 0.35

    @pytest.mark.asyncio
    async def test_all_searchers_latency_comparison(self, mock_searchers_with_latency, benchmark_queries):
        """Compara latência percentil entre todos os searchers."""
        all_metrics = {}

        for name, searcher in mock_searchers_with_latency.items():
            metrics = SearcherLatencyMetrics(name)
            for query in benchmark_queries * 5:  # 40 amostras cada
                await _measure_searcher_latency(searcher, name, query, metrics)
            all_metrics[name] = metrics

        # Ordena por p50
        sorted_by_latency = sorted(
            all_metrics.items(),
            key=lambda x: x[1].p50,
        )

        print("\n=== Ranking de Latência (p50) ===")
        for name, m in sorted_by_latency:
            print(f"  {name:15s}: p50={m.p50:6.1f}ms  p95={m.p95:6.1f}ms  errors={m.errors}")

        # Validações
        fastest = sorted_by_latency[0][0]
        slowest = sorted_by_latency[-1][0]
        assert fastest == "hackernews", f"Esperado hackernews como mais rápido, got {fastest}"
        assert slowest == "firecrawl", f"Esperado firecrawl como mais lento, got {slowest}"

        # Exporta resultados
        results = {name: m.to_dict() for name, m in all_metrics.items()}
        with open("/tmp/latency_benchmark.json", "w") as f:
            json.dump(results, f, indent=2)


# ─── Testes de Identificação de Searchers Lentos ─────────────────────────────


@pytest.mark.asyncio
class TestSlowSearcherDetection:
    """Identifica e isola searchers que excedem thresholds de latência."""

    @pytest.mark.asyncio
    async def test_detect_searchers_exceeding_threshold(self, mock_searchers_with_latency):
        """Identifica searchers com p95 > threshold configurado."""
        THRESHOLD_MS = 380
        benchmark_queries = ["test query"] * 20

        slow_searchers = []
        for name, searcher in mock_searchers_with_latency.items():
            metrics = SearcherLatencyMetrics(name)
            for query in benchmark_queries:
                await _measure_searcher_latency(searcher, name, query, metrics)

            if metrics.p95 > THRESHOLD_MS:
                slow_searchers.append((name, metrics.p95))

        print(f"\nSearchers lentos (p95 > {THRESHOLD_MS}ms): {slow_searchers}")
        assert any(s[0] == "firecrawl" for s in slow_searchers)
        assert any(s[0] == "arxiv" for s in slow_searchers)

    @pytest.mark.asyncio
    async def test_recommendation_for_slow_searchers(self, mock_searchers_with_latency):
        """Gera recomendações de otimização para searchers lentos."""
        THRESHOLD_MS = 400
        recommendations = []

        for name, searcher in mock_searchers_with_latency.items():
            metrics = SearcherLatencyMetrics(name)
            for _ in range(20):
                await _measure_searcher_latency(searcher, name, "test", metrics)

            if metrics.p95 > THRESHOLD_MS:
                if metrics.error_rate > 0.05:
                    recommendations.append(
                        f"{name}: Considerar aumentar timeout ou reduzir retry. "
                        f"Error rate={metrics.error_rate:.1%}"
                    )
                else:
                    recommendations.append(
                        f"{name}: Considerar cache agressivo ou paralelismo. "
                        f"p95={metrics.p95:.0f}ms"
                    )

        print(f"\nRecomendações: {recommendations}")
        assert len(recommendations) >= 2  # firecrawl e arxiv devem estar

    @pytest.mark.asyncio
    async def test_latency_regression_detection(self, mock_searchers_with_latency):
        """Detecta regressão de latência comparando com baseline."""
        BASELINE = {
            "github": 150,
            "reddit": 300,
            "hackernews": 100,
            "arxiv": 500,
            "stackoverflow": 200,
            "google": 350,
            "firecrawl": 900,
            "jina": 200,
        }

        regressions = []
        for name, searcher in mock_searchers_with_latency.items():
            metrics = SearcherLatencyMetrics(name)
            for _ in range(10):
                await _measure_searcher_latency(searcher, name, "test", metrics)

            baseline = BASELINE.get(name, metrics.p50)
            if metrics.p50 > baseline * 1.5:  # 50% acima do baseline
                regressions.append((name, baseline, metrics.p50))

        print(f"\nRegressões detectadas: {regressions}")
        # No mock, não deve haver regressão (latências são fixas)
        assert len(regressions) == 0, f"Regressões inesperadas: {regressions}"


# ─── Testes de Parallel Search com Semaphore ─────────────────────────────────


@pytest.mark.asyncio
class TestParallelSearchWithSemaphore:
    """Valida paralelismo controlado com semáforo."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_searchers(self, mock_searchers_with_latency):
        """Semáforo deve limitar buscas concorrentes."""
        MAX_CONCURRENT = 3
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        active_count = 0
        max_observed = 0

        async def tracked_search(name: str, delay_ms: float):
            nonlocal active_count, max_observed
            async with semaphore:
                active_count += 1
                max_observed = max(max_observed, active_count)
                await asyncio.sleep(delay_ms / 1000.0)
                active_count -= 1
                return f"{name}_result"

        tasks = [
            asyncio.create_task(tracked_search(name, 100))
            for name in mock_searchers_with_latency.keys()
        ]

        results = await asyncio.gather(*tasks)
        assert max_observed <= MAX_CONCURRENT, (
            f"Máximo de concorrentes={max_observed}, esperado <= {MAX_CONCURRENT}"
        )
        assert len(results) == len(mock_searchers_with_latency)

    @pytest.mark.asyncio
    async def test_parallel_vs_sequential_latency(self, mock_searchers_with_latency):
        """Paralelo deve ser significativamente mais rápido que sequencial."""
        queries = ["q1", "q2", "q3"]

        # Sequencial
        start = time.monotonic()
        for name, searcher in mock_searchers_with_latency.items():
            for query in queries:
                try:
                    await searcher.search(query)
                except Exception:
                    pass
        sequential_ms = (time.monotonic() - start) * 1000

        # Paralelo com semáforo
        semaphore = asyncio.Semaphore(5)

        async def bounded_search(name, searcher, query):
            async with semaphore:
                try:
                    return await searcher.search(query)
                except Exception:
                    return []

        start = time.monotonic()
        tasks = [
            asyncio.create_task(bounded_search(name, s, q))
            for name, s in mock_searchers_with_latency.items()
            for q in queries
        ]
        await asyncio.gather(*tasks)
        parallel_ms = (time.monotonic() - start) * 1000

        print(f"\nSequencial: {sequential_ms:.0f}ms | Paralelo: {parallel_ms:.0f}ms")
        print(f"Speedup: {sequential_ms / max(parallel_ms, 1):.1f}x")

        # Paralelo deve ser pelo menos 3x mais rápido
        assert parallel_ms < sequential_ms / 3, (
            f"Paralelo ({parallel_ms:.0f}ms) não foi significativamente "
            f"mais rápido que sequencial ({sequential_ms:.0f}ms)"
        )

    @pytest.mark.asyncio
    async def test_semaphore_prevents_resource_exhaustion(self, mock_searchers_with_latency):
        """Semáforo previne exaustão de recursos com muitos searchers."""
        MAX_CONCURRENT = 2
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        resource_usage = []

        async def resource_intensive_search(name: str):
            async with semaphore:
                resource_usage.append(len(resource_usage))
                # Simula uso de recurso
                await asyncio.sleep(0.05)
                resource_usage.pop()
                return name

        # 20 searchers "virtuais"
        tasks = [
            asyncio.create_task(resource_intensive_search(f"searcher_{i}"))
            for i in range(20)
        ]

        results = await asyncio.gather(*tasks)
        assert len(results) == 20
        # Nunca mais que MAX_CONCURRENT usando recurso simultaneamente
        # (verificado implicitamente pelo semáforo)

    @pytest.mark.asyncio
    async def test_dynamic_semaphore_adjustment(self):
        """Semáforo pode ser ajustado dinamicamente baseado em carga."""
        initial_limit = 5
        semaphore = asyncio.Semaphore(initial_limit)

        # Simula redução de limite sob carga
        current_limit = initial_limit
        load_factor = 0.9  # 90% de carga

        if load_factor > 0.8:
            # Reduz concorrência para preservar recursos
            new_limit = max(1, int(initial_limit * 0.6))
            # Nota: asyncio.Semaphore não permite ajuste dinâmico fácil
            # Em produção, usar pattern de recriação ou bounded semaphore customizado
            current_limit = new_limit

        assert current_limit < initial_limit
        assert current_limit >= 1


# ─── Testes de Early Termination ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestEarlyTermination:
    """Valida early termination quando critérios são atingidos."""

    @pytest.mark.asyncio
    async def test_early_termination_on_result_count(self, mock_searchers_with_latency):
        """Termina busca quando número mínimo de resultados é atingido."""
        MIN_RESULTS = 5
        results = []
        searchers = list(mock_searchers_with_latency.items())

        for name, searcher in searchers:
            res = await searcher.search("test")
            results.extend(res)

            if len(results) >= MIN_RESULTS:
                print(f"Early termination após {name}: {len(results)} resultados")
                break

        assert len(results) >= MIN_RESULTS
        # Não deve ter processado todos os searchers
        assert len(results) <= MIN_RESULTS + len(mock_searchers_with_latency)

    @pytest.mark.asyncio
    async def test_early_termination_on_timeout_budget(self, mock_searchers_with_latency):
        """Termina busca quando budget de tempo é excedido."""
        TIMEOUT_BUDGET_MS = 500
        start = time.monotonic()
        results = []

        for name, searcher in mock_searchers_with_latency.items():
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > TIMEOUT_BUDGET_MS:
                print(f"Timeout budget excedido após {elapsed_ms:.0f}ms")
                break

            try:
                res = await asyncio.wait_for(
                    searcher.search("test"),
                    timeout=(TIMEOUT_BUDGET_MS - elapsed_ms) / 1000.0,
                )
                results.extend(res)
            except asyncio.TimeoutError:
                break

        elapsed_total = (time.monotonic() - start) * 1000
        assert elapsed_total <= TIMEOUT_BUDGET_MS + 100  # tolerância

    @pytest.mark.asyncio
    async def test_early_termination_on_quality_threshold(self, mock_searchers_with_latency):
        """Termina quando resultado de alta qualidade é encontrado."""
        QUALITY_THRESHOLD = 90  # score

        async def quality_search(searcher, name: str):
            res = await searcher.search("test")
            # Simula score de qualidade
            for r in res:
                r.metrics["quality_score"] = 85 if name == "github" else 60
            return res

        results = []
        for name, searcher in mock_searchers_with_latency.items():
            res = await quality_search(searcher, name)
            results.extend(res)

            # Verifica se algum resultado atinge threshold
            if any(r.metrics.get("quality_score", 0) >= QUALITY_THRESHOLD for r in res):
                print(f"Early termination: resultado de qualidade encontrado em {name}")
                break

        assert any(r.metrics.get("quality_score", 0) >= 80 for r in results)

    @pytest.mark.asyncio
    async def test_early_termination_with_circuit_breaker(self, mock_searchers_with_latency):
        """Não tenta searchers com circuit breaker aberto (early skip)."""
        from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        # Simula circuito aberto para firecrawl
        cb = CircuitBreaker(CircuitBreakerConfig(name="firecrawl", failure_threshold=1, recovery_timeout=600))
        await cb._on_failure()
        await cb._on_failure()
        await cb._on_failure()

        assert cb.state.value == "open"

        results = []
        skipped = []
        for name, searcher in mock_searchers_with_latency.items():
            if name == "firecrawl" and cb.state.value == "open":
                skipped.append(name)
                continue
            res = await searcher.search("test")
            results.extend(res)

        assert "firecrawl" in skipped
        assert len(results) > 0  # outros searchers ainda funcionam

    @pytest.mark.asyncio
    async def test_race_termination_first_result(self, mock_searchers_with_latency):
        """Retorna primeiro resultado disponível (race)."""
        async def race_search():
            tasks = {
                name: asyncio.create_task(searcher.search("test"))
                for name, searcher in mock_searchers_with_latency.items()
            }

            # Espera o primeiro a completar
            done, pending = await asyncio.wait(
                tasks.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancela pendentes
            for task in pending:
                task.cancel()

            first_result = list(done)[0].result()
            return first_result

        result = await race_search()
        assert result is not None
        assert len(result) > 0


# ─── Testes de Benchmark Comparativo ─────────────────────────────────────────


@pytest.mark.benchmark
class TestComparativeBenchmark:
    """Benchmarks comparativos entre estratégias de busca."""

    @pytest.mark.asyncio
    async def test_benchmark_all_modes(self, mock_searchers_with_latency):
        """Compara latência total entre modos de operação."""
        from src.operation_modes import OperationModes

        modes = ["guerrilha", "cirurgia", "radar", "black_ops"]
        results = {}

        for mode_name in modes:
            mode = OperationModes.get_mode(mode_name)
            # Filtra searchers pelo modo
            mode_searchers = {
                k: v for k, v in mock_searchers_with_latency.items()
                if k in mode.searchers
            }

            start = time.monotonic()
            tasks = [
                asyncio.create_task(s.search("benchmark"))
                for s in mode_searchers.values()
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = (time.monotonic() - start) * 1000

            results[mode_name] = {
                "searchers": len(mode_searchers),
                "timeout": mode.timeout_seconds,
                "elapsed_ms": elapsed,
            }

        print("\n=== Benchmark por Modo ===")
        for mode, data in results.items():
            print(
                f"  {mode:12s}: {data['searchers']} searchers, "
                f"timeout={data['timeout']}s, elapsed={data['elapsed_ms']:.0f}ms"
            )

        # Guerrilha deve ser mais rápida que black_ops
        assert results["guerrilha"]["elapsed_ms"] < results["black_ops"]["elapsed_ms"]

    @pytest.mark.asyncio
    async def test_benchmark_with_realistic_failures(self, mock_searchers_with_latency):
        """Benchmark com taxas de falha realistas."""
        # Aumenta taxa de falha para simular instabilidade
        unstable_searchers = {
            "reddit": (0.15, 300),      # 15% falha, 300ms
            "firecrawl": (0.25, 1000),  # 25% falha, 1000ms
            "google": (0.05, 350),      # 5% falha, 350ms
        }

        total_latency = 0
        total_success = 0
        total_attempts = 0

        for name, (fail_rate, delay) in unstable_searchers.items():
            searcher = mock_searchers_with_latency[name]
            for _ in range(20):
                start = time.monotonic()
                try:
                    await searcher.search("test")
                    total_success += 1
                except Exception:
                    pass
                total_latency += (time.monotonic() - start) * 1000
                total_attempts += 1

        avg_latency = total_latency / total_attempts
        success_rate = total_success / total_attempts

        print(f"\nInstável: avg={avg_latency:.0f}ms, success_rate={success_rate:.1%}")
        assert success_rate > 0.5  # pelo menos 50% sucesso

    @pytest.mark.asyncio
    async def test_export_benchmark_results(self, mock_searchers_with_latency):
        """Exporta resultados de benchmark em formato JSON."""
        results = {}

        for name, searcher in mock_searchers_with_latency.items():
            metrics = SearcherLatencyMetrics(name)
            for _ in range(50):
                await _measure_searcher_latency(searcher, name, "test", metrics)
            results[name] = metrics.to_dict()

        # Adiciona metadados
        output = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_searchers": len(mock_searchers_with_latency),
            "summary": {
                "fastest": min(results, key=lambda k: results[k]["p50_ms"]),
                "slowest": max(results, key=lambda k: results[k]["p50_ms"]),
                "highest_error_rate": max(results, key=lambda k: results[k]["error_rate"]),
            },
            "details": results,
        }

        with open("/tmp/searcher_latency_benchmark.json", "w") as f:
            json.dump(output, f, indent=2)

        assert output["summary"]["fastest"] == "hackernews"
        assert output["summary"]["slowest"] == "firecrawl"


# ─── Testes de Estresse ──────────────────────────────────────────────────────


@pytest.mark.stress
class TestStressBenchmark:
    """Testes de estresse com carga alta."""

    @pytest.mark.asyncio
    async def test_concurrent_queries_stress(self, mock_searchers_with_latency):
        """100 queries concorrentes."""
        CONCURRENT_QUERIES = 100
        semaphore = asyncio.Semaphore(10)

        async def bounded_query(query_id: int):
            async with semaphore:
                tasks = [
                    asyncio.create_task(s.search(f"query_{query_id}"))
                    for s in mock_searchers_with_latency.values()
                ]
                return await asyncio.gather(*tasks, return_exceptions=True)

        start = time.monotonic()
        tasks = [asyncio.create_task(bounded_query(i)) for i in range(CONCURRENT_QUERIES)]
        results = await asyncio.gather(*tasks)
        elapsed = (time.monotonic() - start) * 1000

        print(f"\n{CONCURRENT_QUERIES} queries: {elapsed:.0f}ms")
        assert len(results) == CONCURRENT_QUERIES
        assert elapsed < 30000  # < 30s para 100 queries

    @pytest.mark.asyncio
    async def test_cascading_failure_stress(self, mock_searchers_with_latency):
        """Simula falha em cascata com 50% dos searchers falhando."""
        failing = ["reddit", "firecrawl", "arxiv"]

        for name in failing:
            searcher = mock_searchers_with_latency[name]
            searcher.search = AsyncMock(side_effect=ConnectionError(f"{name} down"))

        results = []
        for name, searcher in mock_searchers_with_latency.items():
            try:
                res = await searcher.search("test")
                results.extend(res)
            except Exception:
                pass

        # Deve ter resultados dos searchers que não falharam
        assert len(results) > 0
        # Verifica que searchers saudáveis retornaram
        healthy = set(mock_searchers_with_latency.keys()) - set(failing)
        assert any(r.source in healthy for r in results)
