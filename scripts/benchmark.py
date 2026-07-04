#!/usr/bin/env python3
"""Script de benchmark automatizado para o Smart Research Agent (SRA).

Executa N pesquisas em cada modo de operação, mede custo, latência e qualidade,
e gera relatórios comparativos em múltiplos formatos.

Uso:
    # Benchmark completo (todos os modos)
    python scripts/benchmark.py --all

    # Modos específicos
    python scripts/benchmark.py --modes quick standard

    # Número de queries por modo
    python scripts/benchmark.py --all --queries-per-mode 5

    # Formato de saída
    python scripts/benchmark.py --all --format json
    python scripts/benchmark.py --all --format csv
    python scripts/benchmark.py --all --format markdown
    python scripts/benchmark.py --all --format all

    # Integração CI/CD
    python scripts/benchmark.py --all --ci-mode --threshold-file .benchmark_thresholds.json

    # Comparação com baseline
    python scripts/benchmark.py --all --compare-with results/baseline.json

    # Dry run (sem chamar APIs reais)
    python scripts/benchmark.py --all --dry-run

Variáveis de ambiente:
    SRA_API_URL: URL base da API SRA (default: http://localhost:8000)
    SRA_API_KEY: API key para autenticação
    SRA_BENCHMARK_OUTPUT_DIR: Diretório de saída (default: ./benchmark_results)
    SRA_BENCHMARK_QUERIES_FILE: Arquivo JSON com queries customizadas
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

# ── Configuração ─────────────────────────────────────────────────────────────

DEFAULT_API_URL = os.environ.get("SRA_API_URL", "http://localhost:8000")
DEFAULT_OUTPUT_DIR = os.environ.get("SRA_BENCHMARK_OUTPUT_DIR", "./benchmark_results")
DEFAULT_QUERIES_PER_MODE = 3
DEFAULT_TIMEOUT = 300  # segundos

MODOS_DISPONIVEIS = ["quick", "standard", "deep", "comprehensive"]

QUERIES_PADRAO: Dict[str, List[str]] = {
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
class BenchmarkResult:
    """Resultado de uma pesquisa individual."""

    query: str
    mode: str
    correlation_id: str
    success: bool
    duration_seconds: float
    cost_usd: float
    tokens_input: int
    tokens_output: int
    tokens_total: int
    llm_calls: int
    search_calls: int
    report_length: int
    error: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ModeSummary:
    """Resumo agregado por modo."""

    mode: str
    queries_run: int
    queries_success: int
    queries_failed: int
    success_rate: float
    avg_duration_seconds: float
    p50_duration: float
    p95_duration: float
    p99_duration: float
    min_duration: float
    max_duration: float
    avg_cost_usd: float
    min_cost_usd: float
    max_cost_usd: float
    total_cost_usd: float
    avg_tokens_total: float
    avg_tokens_input: float
    avg_tokens_output: float
    avg_llm_calls: float
    avg_search_calls: float
    avg_report_length: float
    cost_per_1k_tokens: float
    results: List[BenchmarkResult] = field(default_factory=list)


@dataclass
class ComparisonResult:
    """Resultado de comparação com baseline."""

    metric: str
    baseline: float
    current: float
    delta: float
    delta_percent: float
    improved: bool


@dataclass
class BenchmarkReport:
    """Relatório completo de benchmark."""

    version: str = "6.0"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    api_url: str = ""
    total_queries: int = 0
    total_success: int = 0
    total_failed: int = 0
    total_duration_seconds: float = 0.0
    total_cost_usd: float = 0.0
    mode_summaries: Dict[str, ModeSummary] = field(default_factory=dict)
    comparisons: List[ComparisonResult] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Cliente HTTP ────────────────────────────────────────────────────────────

class SRABenchmarkClient:
    """Cliente HTTP para API do SRA com métricas."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = None

    async def __aenter__(self):
        try:
            import aiohttp
            self.session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        except ImportError:
            raise ImportError("aiohttp não instalado. Instale com: pip install aiohttp")
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def research(self, query: str, mode: str) -> Dict[str, Any]:
        """Executa pesquisa e retorna métricas."""
        url = urljoin(self.base_url + "/", "research")
        payload = {"query": query, "mode": mode}

        start = time.monotonic()
        try:
            async with self.session.post(url, json=payload) as response:
                data = await response.json()
                duration = time.monotonic() - start

                return {
                    "success": response.status == 200,
                    "status_code": response.status,
                    "duration_seconds": duration,
                    "data": data,
                    "error": data.get("error", "") if response.status != 200 else "",
                }
        except Exception as e:
            return {
                "success": False,
                "status_code": 0,
                "duration_seconds": time.monotonic() - start,
                "data": {},
                "error": str(e),
            }

    async def research_streaming(self, query: str, mode: str) -> Dict[str, Any]:
        """Executa pesquisa via streaming SSE e coleta métricas."""
        url = urljoin(self.base_url + "/", "research/stream")
        params = {"query": query, "mode": mode}

        start = time.monotonic()
        events = []
        final_report = ""
        metrics = {}

        try:
            async with self.session.get(url, params=params) as response:
                async for line in response.content:
                    decoded = line.decode("utf-8").strip()
                    if decoded.startswith("data: "):
                        try:
                            event = json.loads(decoded[6:])
                            events.append(event)

                            if event.get("type") == "complete":
                                final_report = event.get("data", {}).get("report", "")
                                metrics = event.get("data", {}).get("metrics", {})
                            elif event.get("type") == "error":
                                return {
                                    "success": False,
                                    "duration_seconds": time.monotonic() - start,
                                    "error": event.get("message", "Unknown error"),
                                    "events": events,
                                }
                        except json.JSONDecodeError:
                            continue

            return {
                "success": True,
                "duration_seconds": time.monotonic() - start,
                "report": final_report,
                "metrics": metrics,
                "events": events,
                "error": "",
            }

        except Exception as e:
            return {
                "success": False,
                "duration_seconds": time.monotonic() - start,
                "error": str(e),
                "events": events,
            }


# ── Benchmark Runner ──────────────────────────────────────────────────────────

class BenchmarkRunner:
    """Orquestra a execução de benchmarks."""

    def __init__(
        self,
        client: SRABenchmarkClient,
        output_dir: str,
        dry_run: bool = False,
        streaming: bool = False,
    ):
        self.client = client
        self.output_dir = Path(output_dir)
        self.dry_run = dry_run
        self.streaming = streaming
        self.results: List[BenchmarkResult] = []

    async def run_mode(
        self,
        mode: str,
        queries: List[str],
    ) -> ModeSummary:
        """Executa benchmark para um modo específico."""
        print(f"\n{'='*60}")
        print(f"Modo: {mode.upper()}")
        print(f"Queries: {len(queries)}")
        print(f"{'='*60}")

        mode_results: List[BenchmarkResult] = []

        for i, query in enumerate(queries, 1):
            print(f"  [{i}/{len(queries)}] Query: {query[:60]}...", end=" ", flush=True)

            if self.dry_run:
                result = self._dry_run_result(query, mode)
            else:
                result = await self._execute_research(query, mode)

            mode_results.append(result)
            self.results.append(result)

            status = "OK" if result.success else "FAIL"
            print(f"[{status}] {result.duration_seconds:.1f}s ${result.cost_usd:.4f}")

            if not result.success:
                print(f"     Erro: {result.error[:100]}")

        return self._compute_mode_summary(mode, mode_results)

    async def _execute_research(self, query: str, mode: str) -> BenchmarkResult:
        """Executa uma pesquisa e extrai métricas."""
        correlation_id = f"bench-{mode}-{hash(query) % 10000}-{int(time.time())}"

        if self.streaming:
            response = await self.client.research_streaming(query, mode)
        else:
            response = await self.client.research(query, mode)

        data = response.get("data", {})
        metrics = data.get("metrics", {}) if not self.streaming else response.get("metrics", {})

        return BenchmarkResult(
            query=query,
            mode=mode,
            correlation_id=correlation_id,
            success=response.get("success", False),
            duration_seconds=response.get("duration_seconds", 0.0),
            cost_usd=metrics.get("cost_usd", 0.0),
            tokens_input=metrics.get("tokens_input", 0),
            tokens_output=metrics.get("tokens_output", 0),
            tokens_total=metrics.get("tokens_total", 0),
            llm_calls=metrics.get("llm_calls", 0),
            search_calls=metrics.get("search_calls", 0),
            report_length=len(response.get("report", "") or data.get("report", "")),
            error=response.get("error", ""),
        )

    def _dry_run_result(self, query: str, mode: str) -> BenchmarkResult:
        """Simula resultado para dry-run."""
        mode_multipliers = {"quick": 0.5, "standard": 1.0, "deep": 2.5, "comprehensive": 4.0}
        mult = mode_multipliers.get(mode, 1.0)

        return BenchmarkResult(
            query=query,
            mode=mode,
            correlation_id=f"dry-run-{mode}",
            success=True,
            duration_seconds=10.0 * mult,
            cost_usd=0.15 * mult,
            tokens_input=int(2000 * mult),
            tokens_output=int(1000 * mult),
            tokens_total=int(3000 * mult),
            llm_calls=int(5 * mult),
            search_calls=int(5 * mult),
            report_length=int(2000 * mult),
        )

    def _compute_mode_summary(self, mode: str, results: List[BenchmarkResult]) -> ModeSummary:
        """Computa estatísticas agregadas para um modo."""
        if not results:
            return ModeSummary(mode=mode, queries_run=0, queries_success=0, queries_failed=0, success_rate=0.0, avg_duration_seconds=0.0, p50_duration=0.0, p95_duration=0.0, p99_duration=0.0, min_duration=0.0, max_duration=0.0, avg_cost_usd=0.0, min_cost_usd=0.0, max_cost_usd=0.0, total_cost_usd=0.0, avg_tokens_total=0.0, avg_tokens_input=0.0, avg_tokens_output=0.0, avg_llm_calls=0.0, avg_search_calls=0.0, avg_report_length=0.0, cost_per_1k_tokens=0.0)

        durations = [r.duration_seconds for r in results]
        costs = [r.cost_usd for r in results]
        tokens = [r.tokens_total for r in results]
        success_count = sum(1 for r in results if r.success)

        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        p50_idx = int(n * 0.50)
        p95_idx = int(n * 0.95)
        p99_idx = min(int(n * 0.99), n - 1)

        total_tokens = sum(tokens)
        total_cost = sum(costs)

        return ModeSummary(
            mode=mode,
            queries_run=len(results),
            queries_success=success_count,
            queries_failed=len(results) - success_count,
            success_rate=success_count / len(results),
            avg_duration_seconds=sum(durations) / len(results),
            p50_duration=durations_sorted[p50_idx],
            p95_duration=durations_sorted[p95_idx],
            p99_duration=durations_sorted[p99_idx],
            min_duration=min(durations),
            max_duration=max(durations),
            avg_cost_usd=sum(costs) / len(results),
            min_cost_usd=min(costs),
            max_cost_usd=max(costs),
            total_cost_usd=total_cost,
            avg_tokens_total=sum(tokens) / len(results),
            avg_tokens_input=sum(r.tokens_input for r in results) / len(results),
            avg_tokens_output=sum(r.tokens_output for r in results) / len(results),
            avg_llm_calls=sum(r.llm_calls for r in results) / len(results),
            avg_search_calls=sum(r.search_calls for r in results) / len(results),
            avg_report_length=sum(r.report_length for r in results) / len(results),
            cost_per_1k_tokens=(total_cost / total_tokens * 1000) if total_tokens > 0 else 0.0,
            results=results,
        )

    def compare_with_baseline(
        self,
        baseline_path: str,
        current_report: BenchmarkReport,
    ) -> List[ComparisonResult]:
        """Compara resultados atuais com baseline."""
        comparisons = []

        if not Path(baseline_path).exists():
            print(f"[WARNING] Baseline não encontrado: {baseline_path}")
            return comparisons

        with open(baseline_path) as f:
            baseline = json.load(f)

        for mode in MODOS_DISPONIVEIS:
            if mode not in current_report.mode_summaries or mode not in baseline.get("mode_summaries", {}):
                continue

            current = current_report.mode_summaries[mode]
            base = baseline["mode_summaries"][mode]

            for metric in ["avg_duration_seconds", "avg_cost_usd", "avg_tokens_total"]:
                current_val = getattr(current, metric)
                baseline_val = base.get(metric, 0)

                if baseline_val > 0:
                    delta = current_val - baseline_val
                    delta_percent = (delta / baseline_val) * 100
                    improved = delta < 0 if metric != "avg_tokens_total" else delta < 0

                    comparisons.append(ComparisonResult(
                        metric=f"{mode}.{metric}",
                        baseline=baseline_val,
                        current=current_val,
                        delta=delta,
                        delta_percent=delta_percent,
                        improved=improved,
                    ))

        return comparisons


# ── Report Generators ─────────────────────────────────────────────────────────

class ReportGenerator:
    """Gera relatórios em múltiplos formatos."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_json(self, report: BenchmarkReport, filename: str = "benchmark_report.json") -> str:
        """Gera relatório JSON."""
        path = self.output_dir / filename

        data = {
            "version": report.version,
            "timestamp": report.timestamp,
            "api_url": report.api_url,
            "total_queries": report.total_queries,
            "total_success": report.total_success,
            "total_failed": report.total_failed,
            "total_duration_seconds": report.total_duration_seconds,
            "total_cost_usd": report.total_cost_usd,
            "mode_summaries": {
                mode: {
                    "queries_run": s.queries_run,
                    "queries_success": s.queries_success,
                    "queries_failed": s.queries_failed,
                    "success_rate": round(s.success_rate * 100, 1),
                    "avg_duration_seconds": round(s.avg_duration_seconds, 2),
                    "p50_duration": round(s.p50_duration, 2),
                    "p95_duration": round(s.p95_duration, 2),
                    "p99_duration": round(s.p99_duration, 2),
                    "min_duration": round(s.min_duration, 2),
                    "max_duration": round(s.max_duration, 2),
                    "avg_cost_usd": round(s.avg_cost_usd, 4),
                    "min_cost_usd": round(s.min_cost_usd, 4),
                    "max_cost_usd": round(s.max_cost_usd, 4),
                    "total_cost_usd": round(s.total_cost_usd, 4),
                    "avg_tokens_total": round(s.avg_tokens_total, 0),
                    "avg_tokens_input": round(s.avg_tokens_input, 0),
                    "avg_tokens_output": round(s.avg_tokens_output, 0),
                    "avg_llm_calls": round(s.avg_llm_calls, 1),
                    "avg_search_calls": round(s.avg_search_calls, 1),
                    "avg_report_length": round(s.avg_report_length, 0),
                    "cost_per_1k_tokens": round(s.cost_per_1k_tokens, 4),
                    "results": [
                        {
                            "query": r.query,
                            "success": r.success,
                            "duration_seconds": round(r.duration_seconds, 2),
                            "cost_usd": round(r.cost_usd, 4),
                            "tokens_total": r.tokens_total,
                            "llm_calls": r.llm_calls,
                            "error": r.error,
                        }
                        for r in s.results
                    ],
                }
                for mode, s in report.mode_summaries.items()
            },
            "comparisons": [
                {
                    "metric": c.metric,
                    "baseline": round(c.baseline, 4),
                    "current": round(c.current, 4),
                    "delta": round(c.delta, 4),
                    "delta_percent": round(c.delta_percent, 2),
                    "improved": c.improved,
                }
                for c in report.comparisons
            ],
            "metadata": report.metadata,
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return str(path)

    def generate_csv(self, report: BenchmarkReport, filename: str = "benchmark_results.csv") -> str:
        """Gera CSV com resultados individuais."""
        path = self.output_dir / filename

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "query", "mode", "success", "duration_seconds", "cost_usd",
                "tokens_input", "tokens_output", "tokens_total",
                "llm_calls", "search_calls", "report_length", "error", "timestamp",
            ])

            for mode, summary in report.mode_summaries.items():
                for r in summary.results:
                    writer.writerow([
                        r.query, r.mode, r.success, r.duration_seconds, r.cost_usd,
                        r.tokens_input, r.tokens_output, r.tokens_total,
                        r.llm_calls, r.search_calls, r.report_length,
                        r.error, r.timestamp,
                    ])

        return str(path)

    def generate_markdown(self, report: BenchmarkReport, filename: str = "benchmark_report.md") -> str:
        """Gera relatório Markdown."""
        path = self.output_dir / filename

        lines = [
            "# Relatório de Benchmark — Smart Research Agent",
            "",
            f"**Versão:** {report.version}  ",
            f"**Data:** {report.timestamp}  ",
            f"**API:** {report.api_url}  ",
            "",
            "## Resumo",
            "",
            f"| Métrica | Valor |",
            f"|---|---|",
            f"| Total de queries | {report.total_queries} |",
            f"| Sucessos | {report.total_success} |",
            f"| Falhas | {report.total_failed} |",
            f"| Taxa de sucesso | {(report.total_success / max(report.total_queries, 1)) * 100:.1f}% |",
            f"| Custo total | ${report.total_cost_usd:.4f} |",
            f"| Duração total | {report.total_duration_seconds:.1f}s |",
            "",
            "## Resultados por Modo",
            "",
        ]

        for mode, summary in report.mode_summaries.items():
            lines.extend([
                f"### {mode.upper()}",
                "",
                f"| Métrica | Valor |",
                f"|---|---|",
                f"| Queries | {summary.queries_run} |",
                f"| Sucessos | {summary.queries_success} |",
                f"| Taxa de sucesso | {summary.success_rate * 100:.1f}% |",
                f"| Duração média | {summary.avg_duration_seconds:.2f}s |",
                f"| Duração p50 | {summary.p50_duration:.2f}s |",
                f"| Duração p95 | {summary.p95_duration:.2f}s |",
                f"| Custo médio | ${summary.avg_cost_usd:.4f} |",
                f"| Custo mín | ${summary.min_cost_usd:.4f} |",
                f"| Custo máx | ${summary.max_cost_usd:.4f} |",
                f"| Tokens médios | {summary.avg_tokens_total:.0f} |",
                f"| LLM calls médio | {summary.avg_llm_calls:.1f} |",
                f"| Custo/1K tokens | ${summary.cost_per_1k_tokens:.4f} |",
                "",
            ])

        if report.comparisons:
            lines.extend([
                "## Comparação com Baseline",
                "",
                f"| Métrica | Baseline | Atual | Delta | % | Status |",
                f"|---|---|---|---|---|---|",
            ])
            for c in report.comparisons:
                status = "✅ Melhor" if c.improved else "⚠️ Pior"
                lines.append(
                    f"| {c.metric} | {c.baseline:.4f} | {c.current:.4f} | "
                    f"{c.delta:+.4f} | {c.delta_percent:+.1f}% | {status} |"
                )
            lines.append("")

        lines.extend([
            "---",
            "*Gerado automaticamente pelo SRA Benchmark*",
        ])

        with open(path, "w") as f:
            f.write("\n".join(lines))

        return str(path)

    def generate_console_summary(self, report: BenchmarkReport) -> str:
        """Gera resumo para console."""
        lines = [
            "",
            "+--------------------------------------------------------------+",
            "|           BENCHMARK SRA v6.0 -- RESUMO                        |",
            "+--------------------------------------------------------------+",
            f"| Queries: {report.total_queries:3d}  |  Sucesso: {report.total_success:3d}  |  Falha: {report.total_failed:3d}       |",
            f"| Custo total: ${report.total_cost_usd:8.4f}                           |",
            f"| Duração total: {report.total_duration_seconds:6.1f}s                                |",
            "+--------------------------------------------------------------+",
        ]

        for mode, summary in report.mode_summaries.items():
            lines.extend([
                f"| {mode.upper():14s} | {summary.queries_run:2d} queries | "
                f"{summary.success_rate * 100:5.1f}% ok | ${summary.avg_cost_usd:6.4f} | "
                f"{summary.avg_duration_seconds:5.1f}s |",
            ])

        lines.extend([
            "+--------------------------------------------------------------+",
            "",
        ])

        return "\n".join(lines)


# ── CI/CD Integration ─────────────────────────────────────────────────────────

class CICDIntegration:
    """Integração com pipelines CI/CD."""

    def __init__(self, threshold_file: Optional[str] = None):
        self.threshold_file = threshold_file
        self.thresholds: Dict[str, Dict[str, float]] = {}

        if threshold_file and Path(threshold_file).exists():
            with open(threshold_file) as f:
                self.thresholds = json.load(f)

    def check_thresholds(self, report: BenchmarkReport) -> Tuple[bool, List[str]]:
        """Verifica se resultados estão dentro dos thresholds."""
        if not self.thresholds:
            return True, []

        passed = True
        violations = []

        for mode, summary in report.mode_summaries.items():
            mode_thresholds = self.thresholds.get(mode, {})

            if "max_cost_usd" in mode_thresholds:
                if summary.avg_cost_usd > mode_thresholds["max_cost_usd"]:
                    passed = False
                    violations.append(
                        f"[{mode}] Custo médio ${summary.avg_cost_usd:.4f} excede "
                        f"threshold ${mode_thresholds['max_cost_usd']:.4f}"
                    )

            if "max_duration_seconds" in mode_thresholds:
                if summary.avg_duration_seconds > mode_thresholds["max_duration_seconds"]:
                    passed = False
                    violations.append(
                        f"[{mode}] Duração média {summary.avg_duration_seconds:.1f}s excede "
                        f"threshold {mode_thresholds['max_duration_seconds']}s"
                    )

            if "min_success_rate" in mode_thresholds:
                if summary.success_rate < mode_thresholds["min_success_rate"]:
                    passed = False
                    violations.append(
                        f"[{mode}] Taxa de sucesso {summary.success_rate * 100:.1f}% abaixo de "
                        f"{mode_thresholds['min_success_rate'] * 100:.1f}%"
                    )

        return passed, violations

    def generate_github_actions_output(self, report: BenchmarkReport, passed: bool) -> str:
        """Gera output no formato esperado por GitHub Actions."""
        lines = [
            "::group::SRA Benchmark Results",
            f"benchmark_status={'passed' if passed else 'failed'}",
            f"total_queries={report.total_queries}",
            f"total_cost_usd={report.total_cost_usd:.4f}",
            f"success_rate={(report.total_success / max(report.total_queries, 1)) * 100:.1f}",
            "::endgroup::",
        ]
        return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────

def load_custom_queries(file_path: str) -> Dict[str, List[str]]:
    """Carrega queries customizadas de arquivo JSON."""
    if not Path(file_path).exists():
        return {}
    with open(file_path) as f:
        return json.load(f)


async def main():
    parser = argparse.ArgumentParser(
        description="Benchmark automatizado do Smart Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  %(prog)s --all
  %(prog)s --modes quick standard --queries-per-mode 5
  %(prog)s --all --format json --output-dir ./results
  %(prog)s --all --ci-mode --threshold-file .thresholds.json
  %(prog)s --all --compare-with baseline.json
        """,
    )

    parser.add_argument("--all", action="store_true", help="Executa todos os modos")
    parser.add_argument("--modes", nargs="+", choices=MODOS_DISPONIVEIS, help="Modos a executar")
    parser.add_argument("--queries-per-mode", type=int, default=DEFAULT_QUERIES_PER_MODE, help="Queries por modo")
    parser.add_argument("--queries-file", help="Arquivo JSON com queries customizadas")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="URL base da API SRA")
    parser.add_argument("--api-key", default=os.environ.get("SRA_API_KEY"), help="API key")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Diretório de saída")
    parser.add_argument("--format", choices=["json", "csv", "markdown", "all"], default="all", help="Formato do relatório")
    parser.add_argument("--streaming", action="store_true", help="Usa endpoint de streaming SSE")
    parser.add_argument("--dry-run", action="store_true", help="Simula execução sem chamar APIs")
    parser.add_argument("--compare-with", help="Compara com baseline existente")
    parser.add_argument("--ci-mode", action="store_true", help="Modo CI/CD com thresholds")
    parser.add_argument("--threshold-file", help="Arquivo JSON com thresholds")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout por requisição")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output verboso")

    args = parser.parse_args()

    # Determina modos
    if args.all:
        modes = MODOS_DISPONIVEIS
    elif args.modes:
        modes = args.modes
    else:
        parser.error("Especifique --all ou --modes")

    # Carrega queries
    queries_source = QUERIES_PADRAO
    if args.queries_file:
        custom = load_custom_queries(args.queries_file)
        queries_source.update(custom)

    # Limita queries
    queries_by_mode = {
        mode: queries_source.get(mode, [])[:args.queries_per_mode]
        for mode in modes
    }

    print("=" * 70)
    print("  SRA BENCHMARK v6.0")
    print("=" * 70)
    print(f"API: {args.api_url}")
    print(f"Modos: {', '.join(modes)}")
    print(f"Queries por modo: {args.queries_per_mode}")
    print(f"Total estimado: {sum(len(q) for q in queries_by_mode.values())} pesquisas")
    print(f"Output: {args.output_dir}")
    print(f"Formato: {args.format}")
    if args.dry_run:
        print("[WARNING] MODO DRY-RUN (sem chamadas reais)")
    print("=" * 70)

    # Executa benchmark
    async with SRABenchmarkClient(args.api_url, args.api_key, args.timeout) as client:
        runner = BenchmarkRunner(
            client=client,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            streaming=args.streaming,
        )

        report = BenchmarkReport(
            api_url=args.api_url,
            metadata={
                "dry_run": args.dry_run,
                "streaming": args.streaming,
                "queries_per_mode": args.queries_per_mode,
            },
        )

        for mode in modes:
            if not queries_by_mode[mode]:
                print(f"[WARNING] Sem queries para modo '{mode}' -- pulando")
                continue

            summary = await runner.run_mode(mode, queries_by_mode[mode])
            report.mode_summaries[mode] = summary

            report.total_queries += summary.queries_run
            report.total_success += summary.queries_success
            report.total_failed += summary.queries_failed
            report.total_duration_seconds += sum(r.duration_seconds for r in summary.results)
            report.total_cost_usd += summary.total_cost_usd

        # Comparação com baseline
        if args.compare_with:
            report.comparisons = runner.compare_with_baseline(args.compare_with, report)

    # Gera relatórios
    generator = ReportGenerator(Path(args.output_dir))
    generated_files = []

    if args.format in ("json", "all"):
        path = generator.generate_json(report)
        generated_files.append(path)
        print(f"[FILE] JSON: {path}")

    if args.format in ("csv", "all"):
        path = generator.generate_csv(report)
        generated_files.append(path)
        print(f"[FILE] CSV: {path}")

    if args.format in ("markdown", "all"):
        path = generator.generate_markdown(report)
        generated_files.append(path)
        print(f"[FILE] Markdown: {path}")

    # Console summary
    print(generator.generate_console_summary(report))

    # CI/CD checks
    if args.ci_mode:
        ci = CICDIntegration(args.threshold_file)
        passed, violations = ci.check_thresholds(report)

        if violations:
            print("[WARNING] VIOLAÇÕES DE THRESHOLD:")
            for v in violations:
                print(f"   - {v}")

        print(ci.generate_github_actions_output(report, passed))

        if not passed:
            sys.exit(1)

    print(f"[OK] Benchmark concluído. Arquivos gerados em: {args.output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
