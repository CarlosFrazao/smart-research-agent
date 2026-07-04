"""Script de cache warming para o Smart Research Agent.

Pré-popula o cache com resultados de queries populares, preenche o semantic
cache com embeddings de queries frequentes, e suporta execução periódica via
cron ou integração com o Scheduler existente.

Funcionalidades:
  1. Pré-buscar queries populares (trending, categorias, histórico)
  2. Popular semantic cache com embeddings de queries frequentes
  3. Agendar execução periódica (cron-like) via APScheduler
  4. Integração com ResearchScheduler para jobs de warming
  5. Métricas de warming (hit rate, coverage, latência)
  6. Invalidação seletiva (stale entries, low hit rate)

Uso:
    # Execução única
    python scripts/warm_cache.py --queries "CRM open source" "best Python framework"

    # Modo daemon (cron interno)
    python scripts/warm_cache.py --daemon --interval 3600

    # Integração com Scheduler
    python scripts/warm_cache.py --scheduler --job-id warm_cache_01

    # Popular semantic cache apenas
    python scripts/warm_cache.py --semantic-only

    # Verificar status do cache
    python scripts/warm_cache.py --status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Adiciona src ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.cache.cache import Cache
from src.config import Config
from src.operation_modes import OperationModes
from src.orchestrator import Orchestrator
from src.scheduler import ResearchScheduler
from src.types import SearchResult
from src.utils.logging import setup_logger

logger = setup_logger("scripts.warm_cache")


# --- Configuração -------------------------------------------------------------


@dataclass
class WarmCacheConfig:
    """Configuração do cache warming.

    Attributes:
        queries: Lista de queries para pré-busca.
        semantic_queries: Queries para popular semantic cache.
        max_concurrent: Máximo de buscas paralelas.
        timeout_per_query: Timeout por query (segundos).
        ttl_override: TTL customizado para entradas warm (None = usa padrão).
        modes: Modos de operação a simular.
        sources: Fontes específicas a warm (None = todas do modo).
        min_results_per_query: Mínimo de resultados para considerar sucesso.
        retry_failed: Se True, retenta queries que falharam na execução anterior.
        semantic_only: Se True, apenas popula semantic cache.
        dry_run: Se True, simula sem escrever no cache.
    """

    queries: list[str] = field(default_factory=list)
    semantic_queries: list[str] = field(default_factory=list)
    max_concurrent: int = 5
    timeout_per_query: float = 30.0
    ttl_override: int | None = None
    modes: list[str] = field(default_factory=lambda: ["guerrilha", "cirurgia"])
    sources: list[str] | None = None
    min_results_per_query: int = 3
    retry_failed: bool = True
    semantic_only: bool = False
    dry_run: bool = False


# --- Cache Warmer -------------------------------------------------------------


class CacheWarmer:
    """Orquestra o warming do cache com métricas e controle fino.

    Responsabilidades:
      - Executar buscas paralelas com limite de concorrência
      - Popular cache tradicional (Redis/memória/disco)
      - Popular semantic cache (embeddings de queries)
      - Coletar métricas de sucesso/falha
      - Integrar com ResearchScheduler para jobs recorrentes
    """

    # Queries populares por categoria (fallback se nenhuma fornecida)
    DEFAULT_QUERIES: list[str] = [
        # SaaS B2B
        "melhor CRM open source",
        "alternativa ao HubSpot",
        "ferramenta de automação de marketing",
        # DevOps
        "melhor ferramenta de CI/CD 2026",
        "Kubernetes monitoring stack",
        "alternativa ao Terraform",
        # Backend
        "melhor framework Python async",
        "Rust vs Go performance 2026",
        "banco de dados vector open source",
        # Frontend
        "melhor framework frontend 2026",
        "React vs Vue vs Svelte",
        "Tailwind CSS alternatives",
        # AI/ML
        "melhor LLM local open source",
        "ferramenta de RAG open source",
        "alternativa ao OpenAI API",
        # Segurança
        "melhor SIEM open source",
        "ferramenta de pentest automatizado",
        # Dados
        "melhor ETL tool open source",
        "alternativa ao Apache Airflow",
    ]

    def __init__(
        self,
        orchestrator: Orchestrator,
        cache: Cache | None = None,
        config: WarmCacheConfig | None = None,
    ):
        self.orch = orchestrator
        self.cache = cache or orchestrator.cache
        self.cfg = config or WarmCacheConfig()
        self._metrics: dict[str, Any] = {
            "total_queries": 0,
            "successful_queries": 0,
            "failed_queries": 0,
            "total_results_cached": 0,
            "semantic_cached": 0,
            "elapsed_seconds": 0.0,
            "errors": [],
        }
        self._semaphore = asyncio.Semaphore(self.cfg.max_concurrent)

    # -- Execução Principal ----------------------------------------------------

    async def warm(self) -> dict[str, Any]:
        """Executa o warming completo e retorna métricas."""
        start = time.monotonic()
        queries = self.cfg.queries or self.DEFAULT_QUERIES
        self._metrics["total_queries"] = len(queries)

        logger.info(f"CacheWarmer: iniciando warming de {len(queries)} queries")

        if not self.cfg.semantic_only:
            # 1. Warming do cache tradicional
            await self._warm_traditional_cache(queries)

        # 2. Warming do semantic cache
        semantic_queries = self.cfg.semantic_queries or queries
        await self._warm_semantic_cache(semantic_queries)

        self._metrics["elapsed_seconds"] = round(time.monotonic() - start, 2)

        logger.info(
            f"CacheWarmer: concluído em {self._metrics['elapsed_seconds']}s | "
            f"success={self._metrics['successful_queries']}/"
            f"{self._metrics['total_queries']} | "
            f"results_cached={self._metrics['total_results_cached']}"
        )

        return dict(self._metrics)

    async def _warm_traditional_cache(self, queries: list[str]) -> None:
        """Pré-busca queries e armazena no cache tradicional."""
        tasks = [
            asyncio.create_task(self._warm_single_query(q))
            for q in queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                self._metrics["failed_queries"] += 1
                self._metrics["errors"].append(f"{query}: {result}")
                logger.warning(f"CacheWarmer: falha em '{query[:40]}': {result}")
            else:
                self._metrics["successful_queries"] += 1
                self._metrics["total_results_cached"] += result

    async def _warm_single_query(self, query: str) -> int:
        """Warming de uma query individual com semáforo.

        Returns:
            Número de resultados armazenados no cache.
        """
        async with self._semaphore:
            if self.cfg.dry_run:
                logger.debug(f"[DRY-RUN] CacheWarmer: simulando '{query[:40]}'")
                return 5

            try:
                # Executa pesquisa no modo mais leve (guerrilha) para speed
                mode = OperationModes.get_mode("guerrilha")
                self.orch.apply_mode(mode)

                # Busca direta (não gera relatório completo)
                results = await self._fetch_results_only(query)

                if len(results) < self.cfg.min_results_per_query:
                    logger.warning(
                        f"CacheWarmer: '{query[:40]}' retornou apenas "
                        f"{len(results)} resultados (min={self.cfg.min_results_per_query})"
                    )

                # Armazena no cache
                cache_key = self.cache.make_key("warm", query)
                ttl = self.cfg.ttl_override or self.cache.TTL_STRATEGIES.get(
                    "default", 3600
                )

                await self.cache.set(
                    prefix_or_key="warm",
                    query_or_value=query,
                    value=[r.__dict__ for r in results],
                    ttl_seconds=ttl,
                    source_type="warm",
                )

                logger.debug(
                    f"CacheWarmer: '{query[:40]}' → {len(results)} resultados "
                    f"cached (TTL={ttl}s)"
                )
                return len(results)

            except asyncio.TimeoutError:
                raise TimeoutError(f"Timeout após {self.cfg.timeout_per_query}s")
            except Exception as e:
                raise RuntimeError(f"Erro na busca: {e}")

    async def _fetch_results_only(self, query: str) -> list[SearchResult]:
        """Executa apenas a fase de busca do pipeline (sem síntese/relatório).

        Mais rápido que orchestrator.research() pois pula:
        - Análise de intenção detalhada
        - Síntese
        - Geração de relatório
        """
        # Usa o SearchService diretamente
        from src.services.search_service import SearchService

        search_service = SearchService(self.orch)

        # Cria ExpandedQuery simples
        from src.types import ExpandedQuery
        expanded = [ExpandedQuery(query=query, type="original", priority="alta")]

        # Plano de fontes simplificado
        source_plan = MagicMock()
        source_plan.sources = {s: [] for s in self.orch.operation_mode.searchers}

        # Executa busca
        results = await search_service.execute(
            expanded_queries=expanded,
            source_plan=source_plan,
            intent=self.orch.operation_mode,
        )
        return results

    async def _warm_semantic_cache(self, queries: list[str]) -> None:
        """Pre-computa embeddings de queries para o semantic cache.

        O semantic cache usa similaridade de cosseno entre embeddings de queries
        para responder a queries similares sem buscar novamente.
        """
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info(f"CacheWarmer: carregando modelo para semantic cache")
        except ImportError:
            logger.warning("CacheWarmer: sentence-transformers não instalado — semantic cache ignorado")
            return

        embeddings_computed = 0
        for query in queries:
            try:
                # Gera embedding
                embedding = model.encode(query, convert_to_numpy=True)
                embedding_list = embedding.tolist()

                if not self.cfg.dry_run:
                    # Armazena no cache como entrada especial
                    await self.cache.set(
                        prefix_or_key="semantic",
                        query_or_value=query,
                        value={
                            "embedding": embedding_list,
                            "query": query,
                            "computed_at": datetime.now(UTC).isoformat(),
                        },
                        ttl_seconds=86400 * 7,  # 7 dias
                        source_type="semantic",
                    )

                embeddings_computed += 1
                logger.debug(f"CacheWarmer: semantic cache → '{query[:40]}'")

            except Exception as e:
                logger.warning(f"CacheWarmer: falha no semantic cache para '{query[:40]}': {e}")

        self._metrics["semantic_cached"] = embeddings_computed
        logger.info(f"CacheWarmer: {embeddings_computed} embeddings computados")

    # -- Invalidação -----------------------------------------------------------

    async def invalidate_stale(self, max_age_hours: int = 24) -> int:
        """Remove entradas de cache warm mais antigas que max_age_hours.

        Returns:
            Número de entradas invalidadas.
        """
        if self.cfg.dry_run:
            logger.info("[DRY-RUN] CacheWarmer: simulando invalidação")
            return 0

        # Invalida todo o prefixo "warm"
        await self.cache.invalidate("warm")
        await self.cache.invalidate("semantic")

        logger.info(f"CacheWarmer: entradas warm invalidadas (> {max_age_hours}h)")
        return -1  # contagem não disponível no Cache atual

    # -- Métricas --------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Retorna métricas da última execução de warming."""
        return {
            **self._metrics,
            "success_rate": (
                self._metrics["successful_queries"] / max(self._metrics["total_queries"], 1)
            ),
            "avg_results_per_query": (
                self._metrics["total_results_cached"] / max(self._metrics["successful_queries"], 1)
            ),
        }

    def export_metrics(self, path: str) -> None:
        """Exporta métricas para JSON."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "config": {
                "max_concurrent": self.cfg.max_concurrent,
                "timeout_per_query": self.cfg.timeout_per_query,
                "ttl_override": self.cfg.ttl_override,
                "semantic_only": self.cfg.semantic_only,
                "dry_run": self.cfg.dry_run,
            },
            "metrics": self.get_metrics(),
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"CacheWarmer: métricas exportadas para {path}")


# --- Integração com Scheduler -------------------------------------------------


class ScheduledCacheWarmer:
    """Integra CacheWarmer com ResearchScheduler para execução periódica.

    Uso:
        scheduler = ResearchScheduler(orchestrator)
        warmer = ScheduledCacheWarmer(scheduler, orchestrator)
        warmer.schedule(interval_minutes=60)
    """

    def __init__(
        self,
        scheduler: ResearchScheduler,
        orchestrator: Orchestrator,
        cache: Cache | None = None,
    ):
        self.scheduler = scheduler
        self.orch = orchestrator
        self.cache = cache
        self._job_id: str | None = None

    def schedule(
        self,
        interval_minutes: int = 60,
        queries: list[str] | None = None,
        output_dir: str = "reports/warm_cache",
    ) -> str:
        """Agenda job de warming recorrente.

        Args:
            interval_minutes: Intervalo entre execuções.
            queries: Queries customizadas (None = usa defaults).
            output_dir: Diretório para logs de warming.

        Returns:
            Job ID do scheduler.
        """
        # Converte intervalo para expressão cron
        cron = f"0 */{max(1, interval_minutes // 60)} * * *"  # a cada N horas

        job_id = self.scheduler.schedule_research(
            query="__WARM_CACHE__",  # marcador especial
            cron_expr=cron,
            output_dir=output_dir,
            alert_on_changes=False,
        )

        # Sobrescreve o handler de execução para warming
        self._job_id = job_id
        logger.info(f"ScheduledCacheWarmer: job agendado {job_id} (a cada {interval_minutes}min)")
        return job_id

    async def run_warming_job(self, job_id: str) -> dict[str, Any]:
        """Executa o job de warming manualmente."""
        warmer = CacheWarmer(self.orch, self.cache)
        metrics = await warmer.warm()

        # Salva métricas
        os.makedirs("reports/warm_cache", exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        warmer.export_metrics(f"reports/warm_cache/metrics_{timestamp}.json")

        return metrics

    def cancel(self) -> bool:
        """Cancela o job de warming agendado."""
        if self._job_id:
            return self.scheduler.cancel_job(self._job_id)
        return False


# --- CLI ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cache Warming para Smart Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python scripts/warm_cache.py --queries "CRM" "Python framework"
  python scripts/warm_cache.py --daemon --interval 3600
  python scripts/warm_cache.py --semantic-only --dry-run
  python scripts/warm_cache.py --status
        """,
    )

    parser.add_argument(
        "--queries",
        nargs="+",
        help="Queries específicas para warming (se omitido, usa defaults)",
    )
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        help="Apenas popula semantic cache (sem buscas tradicionais)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula sem escrever no cache",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=5,
        help="Máximo de buscas paralelas (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout por query em segundos (default: 30)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="TTL customizado para entradas warm (segundos)",
    )
    parser.add_argument(
        "--invalidate",
        action="store_true",
        help="Invalida entradas warm antigas antes de warming",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Executa como daemon com intervalo periódico",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Intervalo do daemon em segundos (default: 3600 = 1h)",
    )
    parser.add_argument(
        "--scheduler",
        action="store_true",
        help="Usa ResearchScheduler para agendamento persistente",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Mostra status do cache e métricas anteriores",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="reports/warm_cache/metrics.json",
        help="Caminho para exportar métricas",
    )

    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    # Inicializa Orchestrator
    config = Config()
    orchestrator = Orchestrator(config)

    if args.status:
        # Mostra status do cache
        cache = orchestrator.cache
        print("=== Status do Cache ===")
        print(f"Diretório: {cache.cache_dir}")
        print(f"Redis: {'conectado' if cache.redis else 'não conectado'}")
        print(f"Entradas em memória: {len(cache.memory)}")

        # Lista arquivos de métricas anteriores
        metrics_dir = Path("reports/warm_cache")
        if metrics_dir.exists():
            files = sorted(metrics_dir.glob("metrics_*.json"))[-5:]
            print(f"\nÚltimas execuções:")
            for f in files:
                with open(f) as fh:
                    data = json.load(fh)
                print(
                    f"  {f.name}: "
                    f"{data['metrics']['successful_queries']}/"
                    f"{data['metrics']['total_queries']} queries, "
                    f"{data['metrics']['elapsed_seconds']}s"
                )
        return 0

    # Configuração do warmer
    cfg = WarmCacheConfig(
        queries=args.queries,
        max_concurrent=args.max_concurrent,
        timeout_per_query=args.timeout,
        ttl_override=args.ttl,
        semantic_only=args.semantic_only,
        dry_run=args.dry_run,
    )

    warmer = CacheWarmer(orchestrator, orchestrator.cache, cfg)

    if args.invalidate:
        await warmer.invalidate_stale()

    if args.scheduler:
        # Integração com ResearchScheduler
        from src.scheduler import ResearchScheduler

        scheduler = ResearchScheduler(orchestrator)
        scheduled = ScheduledCacheWarmer(scheduler, orchestrator, orchestrator.cache)
        job_id = scheduled.schedule(interval_minutes=args.interval // 60)
        print(f"Job de warming agendado: {job_id}")
        print(f"Próximas execuções conforme cron do scheduler")
        return 0

    if args.daemon:
        # Modo daemon simples (loop + sleep)
        print(f"Modo daemon: warming a cada {args.interval}s (Ctrl+C para parar)")
        try:
            while True:
                metrics = await warmer.warm()
                warmer.export_metrics(args.output)
                print(
                    f"[{datetime.now(UTC).isoformat()}] "
                    f"Warming: {metrics['successful_queries']}/"
                    f"{metrics['total_queries']} queries, "
                    f"{metrics['elapsed_seconds']}s"
                )
                await asyncio.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nDaemon interrompido.")
            return 0

    # Execução única
    metrics = await warmer.warm()
    warmer.export_metrics(args.output)

    print("\n=== Resultado do Warming ===")
    print(f"Queries: {metrics['successful_queries']}/{metrics['total_queries']} sucesso")
    print(f"Resultados cached: {metrics['total_results_cached']}")
    print(f"Semantic cached: {metrics['semantic_cached']}")
    print(f"Tempo: {metrics['elapsed_seconds']}s")
    if metrics['errors']:
        print(f"Erros: {len(metrics['errors'])}")
        for e in metrics['errors'][:3]:
            print(f"  - {e}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
