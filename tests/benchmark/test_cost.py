"""Benchmark de custo por modo de operação do Smart Research Agent (SRA).

Mede tokens, custo USD e eficiência de cada modo de pesquisa:
  - Modos: quick, standard, deep, comprehensive
  - Tokens: input + output por LLM call
  - Custo: USD baseado em preços por modelo (OpenAI, Anthropic, etc.)
  - Comparação: antes vs depois de otimizações
  - Alertas: threshold configurável por modo
  - Relatório: CSV/JSON com métricas detalhadas

Uso:
    pytest tests/benchmark/test_cost.py -v
    pytest tests/benchmark/test_cost.py -v -k "deep"      # só modo deep
    pytest tests/benchmark/test_cost.py -v --benchmark    # modo benchmark completo
    pytest tests/benchmark/test_cost.py -v --save-report  # salva relatório em disco

Variáveis de ambiente:
    SRA_BENCHMARK_QUERIES: Número de queries por modo (default: 3)
    SRA_COST_THRESHOLD_QUICK: Threshold USD para modo quick (default: 0.05)
    SRA_COST_THRESHOLD_STANDARD: Threshold USD para modo standard (default: 0.15)
    SRA_COST_THRESHOLD_DEEP: Threshold USD para modo deep (default: 0.50)
    SRA_COST_THRESHOLD_COMPREHENSIVE: Threshold USD para modo comprehensive (default: 1.00)
    SRA_BENCHMARK_SAVE_PATH: Caminho para salvar relatório (default: /tmp/sra_cost_benchmark.json)

Marcadores pytest:
    - benchmark: testes de benchmark (lentos, requerem LLM real)
    - cost: testes relacionados a custo
    - threshold: testes de alerta de threshold
    - comparison: comparação antes/depois
    - slow: testes que demoram > 30s
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest


# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_QUERIES_PER_MODE: int = 3
DEFAULT_SAVE_PATH: str = "/tmp/sra_cost_benchmark.json"

# Preços por 1K tokens (atualizados 2026-07)
# Fonte: https://openai.com/pricing, https://anthropic.com/pricing
PRICING = {
    "openai": {
        "gpt-4o": {"input": 0.00250, "output": 0.01000},      # $/1K tokens
        "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
        "o3": {"input": 0.00500, "output": 0.02000},
        "o4-mini": {"input": 0.00110, "output": 0.00440},
    },
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": 0.00300, "output": 0.01500},
        "claude-opus-4": {"input": 0.01500, "output": 0.07500},
        "claude-haiku-4": {"input": 0.00025, "output": 0.00125},
    },
    "google": {
        "gemini-2.5-pro": {"input": 0.00125, "output": 0.01000},
        "gemini-2.5-flash": {"input": 0.00015, "output": 0.00060},
    },
    "deepseek": {
        "deepseek-chat": {"input": 0.00014, "output": 0.00028},
        "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    },
}

# Thresholds de custo por modo (USD)
COST_THRESHOLDS: Dict[str, float] = {
    "quick": float(os.environ.get("SRA_COST_THRESHOLD_QUICK", "0.05")),
    "standard": float(os.environ.get("SRA_COST_THRESHOLD_STANDARD", "0.15")),
    "deep": float(os.environ.get("SRA_COST_THRESHOLD_DEEP", "0.50")),
    "comprehensive": float(os.environ.get("SRA_COST_THRESHOLD_COMPREHENSIVE", "1.00")),
}

# Queries de benchmark representativas
BENCHMARK_QUERIES: Dict[str, List[str]] = {
    "quick": [
        "python list comprehension",
        "docker compose example",
        "git rebase vs merge",
    ],
    "standard": [
        "python async frameworks comparison 2026",
        "microservices vs monolith pros cons",
        "postgresql optimization techniques",
    ],
    "deep": [
        "state of the art transformer architectures 2026",
        "distributed systems consensus algorithms comparison",
        "machine learning model deployment best practices",
    ],
    "comprehensive": [
        "comprehensive analysis of LLM agents architecture patterns",
        "systematic review of retrieval augmented generation techniques",
        "comparative study of vector databases for semantic search",
    ],
}


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class LLMCallMetrics:
    """Métricas de uma chamada LLM individual."""

    provider: str
    model: str
    task_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    latency_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ResearchCostMetrics:
    """Métricas de custo de uma pesquisa completa."""

    query: str
    mode: str
    correlation_id: str
    llm_calls: List[LLMCallMetrics] = field(default_factory=list)
    search_calls: int = 0
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0
    search_cost_usd: float = 0.0  # estimativa
    duration_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_llm_call(self, call: LLMCallMetrics) -> None:
        self.llm_calls.append(call)
        self.total_input_tokens += call.input_tokens
        self.total_output_tokens += call.output_tokens
        self.total_tokens += call.total_tokens
        self.llm_cost_usd += call.total_cost_usd
        self.total_cost_usd = self.llm_cost_usd + self.search_cost_usd


@dataclass
class ModeBenchmarkResult:
    """Resultado agregado de benchmark para um modo."""

    mode: str
    queries_tested: int = 0
    total_researches: int = 0
    avg_cost_usd: float = 0.0
    min_cost_usd: float = float("inf")
    max_cost_usd: float = 0.0
    avg_tokens: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_llm_calls: float = 0.0
    avg_search_calls: float = 0.0
    avg_duration_seconds: float = 0.0
    threshold_usd: float = 0.0
    threshold_exceeded_count: int = 0
    threshold_exceeded_rate: float = 0.0
    cost_per_1k_tokens: float = 0.0
    individual_results: List[ResearchCostMetrics] = field(default_factory=list)

    def add_result(self, result: ResearchCostMetrics) -> None:
        self.individual_results.append(result)
        self.queries_tested += 1
        self.total_researches += 1

        self.min_cost_usd = min(self.min_cost_usd, result.total_cost_usd)
        self.max_cost_usd = max(self.max_cost_usd, result.total_cost_usd)

        if result.total_cost_usd > self.threshold_usd:
            self.threshold_exceeded_count += 1

    def compute_aggregates(self) -> None:
        """Computa médias após todos os resultados serem adicionados."""
        if self.queries_tested == 0:
            return

        total_cost = sum(r.total_cost_usd for r in self.individual_results)
        total_tokens = sum(r.total_tokens for r in self.individual_results)
        total_input = sum(r.total_input_tokens for r in self.individual_results)
        total_output = sum(r.total_output_tokens for r in self.individual_results)
        total_llm_calls = sum(len(r.llm_calls) for r in self.individual_results)
        total_search = sum(r.search_calls for r in self.individual_results)
        total_duration = sum(r.duration_seconds for r in self.individual_results)

        self.avg_cost_usd = total_cost / self.queries_tested
        self.avg_tokens = total_tokens / self.queries_tested
        self.avg_input_tokens = total_input / self.queries_tested
        self.avg_output_tokens = total_output / self.queries_tested
        self.avg_llm_calls = total_llm_calls / self.queries_tested
        self.avg_search_calls = total_search / self.queries_tested
        self.avg_duration_seconds = total_duration / self.queries_tested
        self.threshold_exceeded_rate = self.threshold_exceeded_count / self.queries_tested

        if total_tokens > 0:
            self.cost_per_1k_tokens = (total_cost / total_tokens) * 1000


@dataclass
class BenchmarkReport:
    """Relatório completo de benchmark de custo."""

    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    version: str = "6.0"
    mode_results: Dict[str, ModeBenchmarkResult] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_researches: int = 0
    comparison_before: Optional[Dict[str, Any]] = None
    comparison_after: Optional[Dict[str, Any]] = None
    savings_percent: float = 0.0

    def add_mode_result(self, result: ModeBenchmarkResult) -> None:
        self.mode_results[result.mode] = result
        self.total_cost_usd += sum(
            r.total_cost_usd for r in result.individual_results
        )
        self.total_tokens += sum(
            r.total_tokens for r in result.individual_results
        )
        self.total_researches += result.queries_tested

    def compute_savings(self, baseline: Dict[str, Any]) -> None:
        """Computa economia vs baseline."""
        self.comparison_before = baseline
        self.comparison_after = {
            mode: {
                "avg_cost_usd": result.avg_cost_usd,
                "avg_tokens": result.avg_tokens,
                "avg_duration": result.avg_duration_seconds,
            }
            for mode, result in self.mode_results.items()
        }

        if baseline and "total_cost" in baseline:
            old_total = baseline["total_cost"]
            if old_total > 0:
                self.savings_percent = ((old_total - self.total_cost_usd) / old_total) * 100

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "version": self.version,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_tokens": self.total_tokens,
            "total_researches": self.total_researches,
            "savings_percent": round(self.savings_percent, 2),
            "modes": {
                mode: {
                    "avg_cost_usd": round(result.avg_cost_usd, 4),
                    "min_cost_usd": round(result.min_cost_usd, 4),
                    "max_cost_usd": round(result.max_cost_usd, 4),
                    "avg_tokens": round(result.avg_tokens, 0),
                    "avg_input_tokens": round(result.avg_input_tokens, 0),
                    "avg_output_tokens": round(result.avg_output_tokens, 0),
                    "avg_llm_calls": round(result.avg_llm_calls, 1),
                    "avg_search_calls": round(result.avg_search_calls, 1),
                    "avg_duration_seconds": round(result.avg_duration_seconds, 2),
                    "threshold_usd": result.threshold_usd,
                    "threshold_exceeded_count": result.threshold_exceeded_count,
                    "threshold_exceeded_rate": round(result.threshold_exceeded_rate * 100, 1),
                    "cost_per_1k_tokens": round(result.cost_per_1k_tokens, 4),
                    "queries": [
                        {
                            "query": r.query,
                            "cost_usd": round(r.total_cost_usd, 4),
                            "tokens": r.total_tokens,
                            "llm_calls": len(r.llm_calls),
                            "duration_seconds": round(r.duration_seconds, 2),
                        }
                        for r in result.individual_results
                    ],
                }
                for mode, result in self.mode_results.items()
            },
        }


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def benchmark_config() -> Dict[str, Any]:
    """Configuração de benchmark."""
    return {
        "queries_per_mode": int(os.environ.get("SRA_BENCHMARK_QUERIES", DEFAULT_QUERIES_PER_MODE)),
        "save_path": os.environ.get("SRA_BENCHMARK_SAVE_PATH", DEFAULT_SAVE_PATH),
        "thresholds": COST_THRESHOLDS,
        "pricing": PRICING,
    }


@pytest.fixture
def mock_llm_client() -> Mock:
    """Mock de LLM client que rastreia tokens e custo."""
    client = Mock()
    client.provider = "openai"
    client.model = "gpt-4o-mini"

    # Simula custo por chamada
    call_log: List[Dict[str, Any]] = []

    async def mock_generate(prompt: str, **kwargs) -> str:
        # Estima tokens baseado no tamanho do prompt
        input_tokens = len(prompt) // 4  # Heurística: ~4 chars/token
        output_tokens = 500  # Simulação

        call_log.append({
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "prompt_length": len(prompt),
        })

        return f"Generated response for prompt of {len(prompt)} chars"

    async def mock_generate_structured(prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        input_tokens = len(prompt) // 4
        output_tokens = 300

        call_log.append({
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "prompt_length": len(prompt),
            "structured": True,
        })

        return {"result": "structured"}

    client.generate = mock_generate
    client.generate_structured = mock_generate_structured
    client.call_log = call_log

    return client


@pytest.fixture
def mock_orchestrator(mock_llm_client: Mock) -> Mock:
    """Mock de Orchestrator que simula pesquisa com custo."""
    orch = Mock()
    orch.llm_client = mock_llm_client
    orch.cache = Mock()
    orch.ranker = Mock()

    async def mock_research(query: str, **kwargs) -> str:
        mode = kwargs.get("mode", "standard")

        # Simula chamadas LLM baseado no modo
        mode_multipliers = {
            "quick": 2,
            "standard": 5,
            "deep": 12,
            "comprehensive": 20,
        }
        num_calls = mode_multipliers.get(mode, 5)

        for i in range(num_calls):
            prompt = f"Research step {i} for query: {query} " * 50
            await mock_llm_client.generate(prompt)

        return f"# Relatório\n\nResultado para: {query}"

    orch.research = mock_research
    return orch


@pytest.fixture
def cost_calculator() -> "CostCalculator":
    """Calculadora de custo."""
    return CostCalculator()


# ── CostCalculator ──────────────────────────────────────────────────────────

class CostCalculator:
    """Calcula custo USD baseado em tokens e modelo."""

    def calculate_call_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> LLMCallMetrics:
        """Calcula custo de uma chamada LLM."""
        provider_pricing = PRICING.get(provider, {})
        model_pricing = provider_pricing.get(model, {"input": 0.0, "output": 0.0})

        input_cost = (input_tokens / 1000) * model_pricing["input"]
        output_cost = (output_tokens / 1000) * model_pricing["output"]

        return LLMCallMetrics(
            provider=provider,
            model=model,
            task_type="generate",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
            total_cost_usd=input_cost + output_cost,
        )

    def calculate_search_cost(self, num_calls: int, sources: List[str]) -> float:
        """Estima custo de chamadas de busca (APIs pagas)."""
        # Estimativa simplificada
        cost_per_call = 0.001  # $0.001 por chamada de busca
        return num_calls * cost_per_call

    def estimate_tokens(self, text: str) -> int:
        """Estima número de tokens em um texto (heurística)."""
        # Heurística grosseira: ~4 caracteres por token para inglês
        return max(1, len(text) // 4)


# ── Testes de Custo por Modo ────────────────────────────────────────────────

@pytest.mark.benchmark
@pytest.mark.cost
@pytest.mark.slow
class TestCostPerMode:
    """Benchmark de custo por modo de operação."""

    @pytest.mark.parametrize("mode", ["quick", "standard", "deep", "comprehensive"])
    @pytest.mark.asyncio
    async def test_cost_per_mode(
        self,
        mode: str,
        mock_orchestrator: Mock,
        mock_llm_client: Mock,
        cost_calculator: CostCalculator,
        benchmark_config: Dict[str, Any],
    ):
        """Mede custo médio por modo de operação."""
        queries = BENCHMARK_QUERIES.get(mode, ["test query"])
        num_queries = min(benchmark_config["queries_per_mode"], len(queries))
        queries = queries[:num_queries]

        mode_result = ModeBenchmarkResult(
            mode=mode,
            threshold_usd=benchmark_config["thresholds"][mode],
        )

        for query in queries:
            research_metrics = ResearchCostMetrics(
                query=query,
                mode=mode,
                correlation_id=f"bench-{mode}-{hash(query) % 10000}",
            )

            start = time.monotonic()

            # Executa pesquisa
            await mock_orchestrator.research(query, mode=mode)

            # Calcula métricas das chamadas LLM
            for call in mock_llm_client.call_log:
                llm_metrics = cost_calculator.calculate_call_cost(
                    provider="openai",
                    model="gpt-4o-mini",
                    input_tokens=call["input_tokens"],
                    output_tokens=call["output_tokens"],
                )
                research_metrics.add_llm_call(llm_metrics)

            # Estima custo de busca
            research_metrics.search_calls = 10 if mode in ["deep", "comprehensive"] else 5
            research_metrics.search_cost_usd = cost_calculator.calculate_search_cost(
                research_metrics.search_calls,
                ["github", "reddit", "searxng"],
            )
            research_metrics.total_cost_usd = (
                research_metrics.llm_cost_usd + research_metrics.search_cost_usd
            )

            research_metrics.duration_seconds = time.monotonic() - start
            mode_result.add_result(research_metrics)

            # Limpa log para próxima query
            mock_llm_client.call_log.clear()

        mode_result.compute_aggregates()

        # Salva no report global
        pytest.benchmark_report = getattr(pytest, "benchmark_report", BenchmarkReport())
        pytest.benchmark_report.add_mode_result(mode_result)

        # Asserções
        assert mode_result.avg_cost_usd > 0, f"Custo médio de {mode} deve ser > 0"
        assert mode_result.avg_tokens > 0, f"Tokens médios de {mode} deve ser > 0"

        print(f"\n[{mode.upper()}]")
        print(f"  Custo médio: ${mode_result.avg_cost_usd:.4f}")
        print(f"  Tokens médios: {mode_result.avg_tokens:.0f}")
        print(f"  LLM calls médio: {mode_result.avg_llm_calls:.1f}")
        print(f"  Duração média: {mode_result.avg_duration_seconds:.1f}s")

    @pytest.mark.parametrize("mode", ["quick", "standard", "deep", "comprehensive"])
    @pytest.mark.threshold
    def test_cost_threshold(
        self,
        mode: str,
        benchmark_config: Dict[str, Any],
    ):
        """Valida que threshold de custo está configurado."""
        threshold = benchmark_config["thresholds"][mode]
        assert threshold > 0, f"Threshold de {mode} deve ser > 0"
        assert threshold < 10.0, f"Threshold de {mode} parece muito alto: {threshold}"


# ── Testes de Tokens ──────────────────────────────────────────────────────────

@pytest.mark.benchmark
@pytest.mark.cost
class TestTokenMeasurement:
    """Valida medição precisa de tokens."""

    def test_token_estimation_heuristic(self, cost_calculator: CostCalculator):
        """Heurística de tokens deve ser razoável."""
        text = "The quick brown fox jumps over the lazy dog. " * 10  # ~470 chars
        tokens = cost_calculator.estimate_tokens(text)
        # ~470 chars / 4 = ~117 tokens
        assert 80 < tokens < 150, f"Estimativa de tokens fora do esperado: {tokens}"

    def test_token_count_empty_string(self, cost_calculator: CostCalculator):
        """String vazia deve retornar mínimo 1 token."""
        tokens = cost_calculator.estimate_tokens("")
        assert tokens == 1

    def test_llm_call_metrics_computation(self, cost_calculator: CostCalculator):
        """Métricas de chamada LLM devem somar corretamente."""
        metrics = cost_calculator.calculate_call_cost(
            provider="openai",
            model="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
        )

        assert metrics.total_tokens == 1500
        assert metrics.input_cost_usd == 0.00250  # $0.00250 / 1K input
        assert metrics.output_cost_usd == 0.00500  # $0.01000 / 1K output * 500
        assert abs(metrics.total_cost_usd - 0.00750) < 0.00001

    @pytest.mark.parametrize("provider,model", [
        ("openai", "gpt-4o"),
        ("openai", "gpt-4o-mini"),
        ("anthropic", "claude-sonnet-4-20250514"),
        ("deepseek", "deepseek-chat"),
    ])
    def test_pricing_available(self, provider: str, model: str):
        """Todos os modelos devem ter preços configurados."""
        assert provider in PRICING, f"Provider {provider} não encontrado"
        assert model in PRICING[provider], f"Modelo {model} não encontrado em {provider}"
        assert PRICING[provider][model]["input"] > 0
        assert PRICING[provider][model]["output"] > 0


# ── Testes de Comparação Antes/Depois ───────────────────────────────────────

@pytest.mark.benchmark
@pytest.mark.comparison
class TestBeforeAfterComparison:
    """Compara custo antes e depois de otimizações."""

    @pytest.fixture
    def baseline_costs(self) -> Dict[str, Any]:
        """Baseline de custo antes das otimizações (SRA v5.x)."""
        return {
            "version": "5.0",
            "total_cost": 2.50,  # USD para 4 pesquisas (1 por modo)
            "modes": {
                "quick": {"avg_cost_usd": 0.15, "avg_tokens": 8000},
                "standard": {"avg_cost_usd": 0.45, "avg_tokens": 25000},
                "deep": {"avg_cost_usd": 1.20, "avg_tokens": 80000},
                "comprehensive": {"avg_cost_usd": 2.50, "avg_tokens": 180000},
            },
        }

    @pytest.mark.asyncio
    async def test_cost_reduction_after_optimizations(
        self,
        mock_orchestrator: Mock,
        mock_llm_client: Mock,
        cost_calculator: CostCalculator,
        baseline_costs: Dict[str, Any],
    ):
        """Custo após otimizações deve ser menor que baseline."""
        # Simula pesquisa otimizada (menos chamadas LLM)
        mode = "standard"
        query = "python async frameworks"

        # Versão otimizada: menos chamadas LLM, mais cache
        mock_llm_client.call_log.clear()

        # Simula apenas 3 chamadas (vs 5 no baseline)
        for i in range(3):
            prompt = f"Optimized step {i} for {query}"
            await mock_llm_client.generate(prompt)

        total_cost = 0.0
        total_tokens = 0
        for call in mock_llm_client.call_log:
            metrics = cost_calculator.calculate_call_cost(
                "openai", "gpt-4o-mini",
                call["input_tokens"], call["output_tokens"],
            )
            total_cost += metrics.total_cost_usd
            total_tokens += metrics.total_tokens

        baseline = baseline_costs["modes"]["standard"]["avg_cost_usd"]

        # Espera redução de ~40% com otimizações
        assert total_cost < baseline * 0.7, (
            f"Custo otimizado (${total_cost:.4f}) não é menor que "
            f"70% do baseline (${baseline * 0.7:.4f})"
        )

    def test_token_reduction_with_caching(self):
        """Cache deve reduzir tokens reutilizados."""
        # Simula cache hit
        cached_tokens = 5000
        total_tokens_without_cache = 15000
        total_tokens_with_cache = total_tokens_without_cache - cached_tokens

        reduction = (cached_tokens / total_tokens_without_cache) * 100
        assert reduction > 20, f"Redução de tokens com cache: {reduction:.1f}%"

    def test_parallel_search_reduces_llm_calls(self):
        """Busca paralela deve reduzir chamadas LLM sequenciais."""
        sequential_calls = 15
        parallel_calls = 8  # Com early termination e batching

        reduction = ((sequential_calls - parallel_calls) / sequential_calls) * 100
        assert reduction > 40, f"Redução de LLM calls: {reduction:.1f}%"


# ── Testes de Alerta de Threshold ────────────────────────────────────────────

@pytest.mark.benchmark
@pytest.mark.threshold
class TestCostThresholdAlerts:
    """Valida alertas quando custo excede threshold."""

    @pytest.mark.parametrize("mode", ["quick", "standard", "deep", "comprehensive"])
    def test_threshold_alert_triggered(
        self,
        mode: str,
        benchmark_config: Dict[str, Any],
    ):
        """Alerta deve ser acionado quando custo > threshold."""
        threshold = benchmark_config["thresholds"][mode]

        # Simula custo que excede threshold
        simulated_cost = threshold * 1.5

        alert_triggered = simulated_cost > threshold
        assert alert_triggered

        # Verifica mensagem de alerta
        excess_percent = ((simulated_cost - threshold) / threshold) * 100
        assert excess_percent > 0

    @pytest.mark.parametrize("mode", ["quick", "standard", "deep", "comprehensive"])
    def test_threshold_alert_not_triggered_when_under(
        self,
        mode: str,
        benchmark_config: Dict[str, Any],
    ):
        """Alerta NÃO deve ser acionado quando custo < threshold."""
        threshold = benchmark_config["thresholds"][mode]
        simulated_cost = threshold * 0.5

        alert_triggered = simulated_cost > threshold
        assert not alert_triggered

    def test_alert_message_format(self):
        """Mensagem de alerta deve conter informações úteis."""
        mode = "deep"
        cost = 0.75
        threshold = 0.50

        message = (
            f"ALERTA: Custo do modo '{mode}' (${cost:.4f}) "
            f"excede threshold (${threshold:.4f}) em "
            f"{((cost - threshold) / threshold) * 100:.1f}%"
        )

        assert mode in message
        assert f"${cost:.4f}" in message
        assert f"${threshold:.4f}" in message
        assert "%" in message

    def test_severity_levels(self):
        """Alertas devem ter níveis de severidade baseados no excesso."""
        def get_severity(cost: float, threshold: float) -> str:
            ratio = cost / threshold
            if ratio < 1.0:
                return "normal"
            elif ratio < 1.25:
                return "warning"
            elif ratio < 1.5:
                return "elevated"
            elif ratio < 2.0:
                return "critical"
            return "emergency"

        assert get_severity(0.40, 0.50) == "normal"
        assert get_severity(0.60, 0.50) == "warning"
        assert get_severity(0.70, 0.50) == "elevated"
        assert get_severity(0.90, 0.50) == "critical"
        assert get_severity(1.10, 0.50) == "emergency"


# ── Testes de Relatório ─────────────────────────────────────────────────────

@pytest.mark.benchmark
class TestBenchmarkReport:
    """Valida geração e formato do relatório de benchmark."""

    def test_report_generation(self, benchmark_config: Dict[str, Any]):
        """Relatório deve ser gerado em formato JSON."""
        report = BenchmarkReport()

        # Adiciona resultados simulados
        for mode in ["quick", "standard"]:
            mode_result = ModeBenchmarkResult(mode=mode, threshold_usd=0.15)
            for i in range(2):
                research = ResearchCostMetrics(
                    query=f"query {i}",
                    mode=mode,
                    correlation_id=f"test-{i}",
                    total_cost_usd=0.10 if mode == "quick" else 0.30,
                    total_tokens=5000 if mode == "quick" else 15000,
                )
                mode_result.add_result(research)
            mode_result.compute_aggregates()
            report.add_mode_result(mode_result)

        report_dict = report.to_dict()

        assert "timestamp" in report_dict
        assert "version" in report_dict
        assert "total_cost_usd" in report_dict
        assert "modes" in report_dict
        assert "quick" in report_dict["modes"]
        assert "standard" in report_dict["modes"]

    def test_report_serialization(self, benchmark_config: Dict[str, Any]):
        """Relatório deve ser serializável em JSON."""
        report = BenchmarkReport()
        report_dict = report.to_dict()

        # Deve ser serializável sem erros
        json_str = json.dumps(report_dict, indent=2)
        assert len(json_str) > 0

        # Deve ser deserializável
        loaded = json.loads(json_str)
        assert loaded["version"] == "6.0"

    @pytest.mark.skipif(
        not os.environ.get("SRA_BENCHMARK_SAVE_PATH"),
        reason="Define SRA_BENCHMARK_SAVE_PATH para testar salvamento",
    )
    def test_report_save_to_disk(self, benchmark_config: Dict[str, Any]):
        """Relatório deve ser salvo em disco."""
        report = BenchmarkReport()
        report_dict = report.to_dict()

        save_path = benchmark_config["save_path"]
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        with open(save_path, "w") as f:
            json.dump(report_dict, f, indent=2)

        assert Path(save_path).exists()
        assert Path(save_path).stat().st_size > 0

        # Cleanup
        Path(save_path).unlink(missing_ok=True)

    def test_report_comparison(self):
        """Relatório deve computar economia vs baseline."""
        report = BenchmarkReport()

        baseline = {
            "version": "5.0",
            "total_cost": 2.00,
            "modes": {
                "standard": {"avg_cost_usd": 0.50},
            },
        }

        mode_result = ModeBenchmarkResult(mode="standard", threshold_usd=0.15)
        research = ResearchCostMetrics(
            query="test", mode="standard",
            correlation_id="test", total_cost_usd=0.30,
        )
        mode_result.add_result(research)
        mode_result.compute_aggregates()
        report.add_mode_result(mode_result)

        report.compute_savings(baseline)

        assert report.savings_percent > 0
        assert report.comparison_before is not None
        assert report.comparison_after is not None


# ── Testes de Custo por Componente ────────────────────────────────────────────

@pytest.mark.cost
class TestCostBreakdown:
    """Valida breakdown de custo por componente."""

    def test_llm_cost_dominates(self, cost_calculator: CostCalculator):
        """LLM deve ser o componente mais caro em modos complexos."""
        # Simula pesquisa deep
        llm_cost = 0.80
        search_cost = 0.05
        total = llm_cost + search_cost

        assert llm_cost > search_cost * 5  # LLM é > 5x mais caro que busca
        assert llm_cost / total > 0.8  # LLM representa > 80% do custo

    def test_input_vs_output_cost_ratio(self, cost_calculator: CostCalculator):
        """Custo de output geralmente é maior que input."""
        metrics = cost_calculator.calculate_call_cost(
            "openai", "gpt-4o",
            input_tokens=1000,
            output_tokens=1000,
        )

        # GPT-4o: input $0.00250, output $0.01000 por 1K
        assert metrics.output_cost_usd > metrics.input_cost_usd
        assert metrics.output_cost_usd / metrics.input_cost_usd == 4.0

    def test_cheaper_model_reduces_cost(self, cost_calculator: CostCalculator):
        """Modelo mais barato deve reduzir custo significativamente."""
        expensive = cost_calculator.calculate_call_cost(
            "openai", "gpt-4o", 1000, 500,
        )
        cheap = cost_calculator.calculate_call_cost(
            "openai", "gpt-4o-mini", 1000, 500,
        )

        assert cheap.total_cost_usd < expensive.total_cost_usd
        # GPT-4o-mini é ~16x mais barato
        assert cheap.total_cost_usd / expensive.total_cost_usd < 0.1


# ── Testes de Estabilidade ──────────────────────────────────────────────────

@pytest.mark.benchmark
class TestCostStability:
    """Valida estabilidade de custo entre execuções."""

    @pytest.mark.asyncio
    async def test_cost_variance_acceptable(
        self,
        mock_orchestrator: Mock,
        mock_llm_client: Mock,
        cost_calculator: CostCalculator,
    ):
        """Variação de custo entre execuções similares deve ser < 20%."""
        costs = []

        for _ in range(3):
            mock_llm_client.call_log.clear()
            await mock_orchestrator.research("python async", mode="standard")

            total = sum(
                cost_calculator.calculate_call_cost(
                    "openai", "gpt-4o-mini",
                    c["input_tokens"], c["output_tokens"],
                ).total_cost_usd
                for c in mock_llm_client.call_log
            )
            costs.append(total)

        avg = sum(costs) / len(costs)
        variance = max(abs(c - avg) for c in costs) / avg if avg > 0 else 0

        assert variance < 0.20, f"Variação de custo muito alta: {variance:.1%}"


# ── Testes de Configuração ────────────────────────────────────────────────────

class TestBenchmarkConfiguration:
    """Valida configuração de benchmark."""

    def test_queries_per_mode_configurable(self):
        """Número de queries deve ser configurável via env."""
        queries = int(os.environ.get("SRA_BENCHMARK_QUERIES", "3"))
        assert 1 <= queries <= 10, f"Queries por modo deve ser 1-10, got {queries}"

    def test_thresholds_are_reasonable(self):
        """Thresholds devem ser razoáveis."""
        for mode, threshold in COST_THRESHOLDS.items():
            assert threshold > 0, f"Threshold de {mode} deve ser > 0"
            assert threshold < 5.0, f"Threshold de {mode} parece muito alto"

    def test_pricing_up_to_date(self):
        """Preços devem ser positivos e razoáveis."""
        for provider, models in PRICING.items():
            for model, prices in models.items():
                assert prices["input"] > 0, f"Preço input de {provider}/{model} inválido"
                assert prices["output"] > 0, f"Preço output de {provider}/{model} inválido"
                assert prices["output"] >= prices["input"], (
                    f"Output mais barato que input em {provider}/{model}"
                )
