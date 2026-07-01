"""
Benchmark E2E do Smart Research Agent.
Requer API keys reais configuradas no .env para rodar com dados reais.
Use --mock para rodar em modo simulado (padrao para CI).
"""
import asyncio
import time
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

BENCHMARK_QUERIES = [
    "CRM open source parecido com HubSpot",
    "melhor stack para automacao de marketing 2026",
    "n8n vs Make vs Zapier",
    "self-hosted alternatives to Notion",
    "best open source LLM for local deployment",
]


async def run_benchmark(mock: bool = False):
    from src.config import Config
    from src.orchestrator import Orchestrator

    config = Config()
    orchestrator = Orchestrator(config)

    if mock:
        from unittest.mock import AsyncMock
        from src.types import SearchResult

        mock_results = [
            SearchResult(
                source=src,
                title=f"tool/{src}-project-{i}",
                url=f"https://github.com/{src}/project{i}",
                description=f"Amazing open source tool {i} for your needs",
                metrics={"stars": 5000 + i * 1000, "forks": 500, "language": "Python",
                         "updated_at": "2026-01-01", "license": "MIT"},
            )
            for i, src in enumerate(["github", "reddit", "hackernews", "awesome"] * 4)
        ]
        orchestrator._parallel_search = AsyncMock(return_value=mock_results)
        orchestrator.llm.generate = AsyncMock(return_value="Sumario executivo de benchmark.")
        orchestrator.llm.generate_structured = AsyncMock(side_effect=Exception("Mock mode"))

    results = []

    for query in BENCHMARK_QUERIES:
        print(f"\nBenchmark: {query}")
        start = time.time()

        try:
            report = await orchestrator.research(query)
            duration = time.time() - start

            has_table = "|" in report and "-" in report
            has_recommendation = "Recomendacao" in report
            has_projects = "2.1" in report or "2.2" in report

            results.append({
                "query": query,
                "duration": duration,
                "success": True,
                "has_table": has_table,
                "has_recommendation": has_recommendation,
                "has_projects": has_projects,
                "report_length": len(report),
            })
            print(f"  OK {round(duration, 1)}s | {len(report)} chars | table={has_table} reco={has_recommendation}")

        except Exception as e:
            duration = time.time() - start
            results.append({"query": query, "duration": duration, "success": False, "error": str(e)})
            print(f"  ERRO {round(duration, 1)}s | {e}")

    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)

    total_time = sum(r["duration"] for r in results)
    success_rate = sum(1 for r in results if r["success"]) / len(results) * 100

    print(f"Total queries: {len(results)}")
    print(f"Success rate: {round(success_rate, 0)}%")
    print(f"Total time: {round(total_time, 1)}s")
    print(f"Avg time: {round(total_time/len(results), 1)}s")

    for r in results:
        status = "OK" if r["success"] else "ERRO"
        print(f"\n{status} {r['query'][:50]}...")
        print(f"   Time: {round(r['duration'], 1)}s")
        if r["success"]:
            print(f"   Length: {r['report_length']} chars")

    all_success = all(r["success"] for r in results)
    return 0 if all_success else 1


if __name__ == "__main__":
    use_mock = "--mock" in sys.argv or not os.path.exists(".env")
    if use_mock:
        print("[MODO MOCK] Rodando benchmark sem API keys reais")
    exit_code = asyncio.run(run_benchmark(mock=use_mock))
    sys.exit(exit_code)
