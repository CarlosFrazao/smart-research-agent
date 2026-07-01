#!/usr/bin/env python3
import argparse
import asyncio
import sys
import logging
from src.orchestrator import Orchestrator
from src.deep_researcher import DeepResearcher
from src.config import Config
from src.utils.logger import setup_logger

logger = setup_logger("main")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart Research Agent - Pesquisa profunda em tecnologia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponiveis")

    research_parser = subparsers.add_parser("research", help="Executar pesquisa")
    research_parser.add_argument("query", help="Query de pesquisa (entre aspas)")
    research_parser.add_argument("--output", "-o", help="Caminho do arquivo de saida")
    research_parser.add_argument("--max-results", type=int, default=20)
    research_parser.add_argument("--iterations", type=int, default=3)
    research_parser.add_argument("--sources", help="Fontes a usar (separadas por virgula)")
    research_parser.add_argument("--verbose", "-v", action="store_true")
    research_parser.add_argument(
        "--mode",
        choices=["standard", "deep"],
        default="standard",
        help="Modo de pesquisa: standard (rapido) ou deep (raciocinio em arvore, ~5-10x mais caro)",
    )

    from src.operation_modes import OperationModes
    research_parser.add_argument(
        "--op-mode",
        choices=OperationModes.list_modes(),
        help="Modo de operacao: guerrilha, cirurgia, radar, arqueologia, concorrencia, black_ops",
    )

    subparsers.add_parser("config", help="Mostrar configuracao atual")
    subparsers.add_parser("version", help="Mostrar versao")

    # ── Schedule subcommands ───────────────────────────────────────────────
    schedule_parser = subparsers.add_parser("schedule", help="Gerenciar pesquisas agendadas")
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command", help="Acao")

    sched_add = schedule_sub.add_parser("add", help="Agendar nova pesquisa recorrente")
    sched_add.add_argument("--query", "-q", required=True, help="Query de pesquisa")
    sched_add.add_argument("--cron", "-c", required=True, help="Expressao cron (ex: '0 9 * * 1')")
    sched_add.add_argument("--output-dir", "-o", default="reports/scheduled", help="Diretorio de saida")
    sched_add.add_argument("--webhook", "-w", help="URL de webhook para alertas (Slack, Discord, N8N)")
    sched_add.add_argument("--no-alerts", action="store_true", help="Desabilitar alertas de mudancas")

    schedule_sub.add_parser("list", help="Listar jobs agendados")

    sched_cancel = schedule_sub.add_parser("cancel", help="Cancelar um job agendado")
    sched_cancel.add_argument("--id", required=True, help="ID do job a cancelar")

    sched_run = schedule_sub.add_parser("run", help="Executar um job agendado imediatamente")
    sched_run.add_argument("--id", required=True, help="ID do job a executar")

    return parser


async def cmd_research(args):
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from src.operation_modes import OperationModes
    op_mode = getattr(args, "op_mode", None)
    if not op_mode:
        op_mode = OperationModes.auto_select(args.query)
        logger.info(f"Modo de operacao auto-selecionado: '{op_mode}'")

    config = Config()
    if args.max_results:
        config.max_results_per_source = args.max_results
    if args.iterations:
        config.max_iterations = args.iterations

    orchestrator = Orchestrator(config)
    orchestrator.operation_mode = OperationModes.get_mode(op_mode)

    mode = getattr(args, "mode", "standard")
    print(f"Iniciando pesquisa: '{args.query}' [modo: {mode}] [op-mode: {op_mode}]")
    print("=" * 60)

    try:
        if mode == "deep":
            deep_researcher = DeepResearcher(
                llm_client=orchestrator.llm,
                orchestrator=orchestrator,
                memory=orchestrator.memory,
            )
            deep_result = await deep_researcher.research(args.query)
            report = (
                f"# Deep Research Report\n\n"
                f"**Query:** {args.query}\n\n"
                f"**Nodes explored:** {deep_result.total_nodes_explored}\n\n"
                f"**Confirmed hypotheses:** {len(deep_result.confirmed_hypotheses)}\n\n"
                + deep_result.reasoning_tree
                + "\n\n## Findings\n\n"
                + "\n".join(
                    f"- [{r.title or '(sem título)'}]({r.url or ''}) — {(r.description or '')[:120]}"
                    for r in deep_result.findings[:20]
                )
            )
        else:
            report = await orchestrator.research(args.query)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\nRelatorio salvo em: {args.output}")
        else:
            print("\n" + "=" * 60)
            sys.stdout.buffer.write((report + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
    except Exception as e:
        logger.error(f"Erro durante pesquisa: {e}")
        sys.exit(1)


def cmd_config(args):
    config = Config()
    print("Configuracao atual:")
    print(f"  LLM Provider: {config.llm_provider}")
    print(f"  Modelo: {config.get_llm_config().get('model', 'N/A')}")
    print(f"  Max resultados/fonte: {config.max_results_per_source}")
    print(f"  Max iteracoes: {config.max_iterations}")
    print(f"  Timeout: {config.timeout_per_source}s")
    print(f"  Output dir: {config.output_dir}")
    print(f"  Cache dir: {config.cache_dir}")


def cmd_version(args):
    print("Smart Research Agent v1.0")
    print("Codinome: TechCurator")
    print("Licenca: MIT")


async def cmd_schedule(args):
    """Gerenciador de pesquisas agendadas via CLI."""
    from src.scheduler import ResearchScheduler
    config = Config()
    orchestrator = Orchestrator(config)
    scheduler = ResearchScheduler(orchestrator)

    subcmd = getattr(args, "schedule_command", None)
    if subcmd == "add":
        job_id = scheduler.schedule_research(
            query=args.query,
            cron_expr=args.cron,
            output_dir=args.output_dir,
            webhook_url=getattr(args, "webhook", None),
            alert_on_changes=not getattr(args, "no_alerts", False),
        )
        print(f"✅ Job agendado com sucesso!")
        print(f"   ID:    {job_id}")
        print(f"   Query: {args.query}")
        print(f"   Cron:  {args.cron}")
        print(f"   Dir:   {args.output_dir}")

    elif subcmd == "list":
        jobs = scheduler.list_jobs()
        if not jobs:
            print("Nenhum job agendado.")
            return
        print(f"{'ID':<38} {'Query':<30} {'Cron':<18} {'Ultimo Run'}")
        print("-" * 100)
        for j in jobs:
            last = j.get("last_run") or "nunca"
            print(f"{j['id']:<38} {j['query'][:28]:<30} {j['cron']:<18} {last[:19]}")

    elif subcmd == "cancel":
        removed = scheduler.cancel_job(args.id)
        if removed:
            print(f"✅ Job '{args.id}' cancelado.")
        else:
            print(f"❌ Job '{args.id}' nao encontrado.")

    elif subcmd == "run":
        print(f"⏳ Executando job '{args.id}'...")
        report = await scheduler.run_scheduled_research(args.id)
        print(f"✅ Job concluido. Relatorio gerado ({len(report)} chars).")

    else:
        print("Use: sra schedule [add|list|cancel|run] --help")


async def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "research":
        await cmd_research(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "version":
        cmd_version(args)
    elif args.command == "schedule":
        await cmd_schedule(args)


if __name__ == "__main__":
    asyncio.run(main())
