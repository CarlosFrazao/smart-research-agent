"""Módulo de Interface de Terminal (CLI) via Typer e Rich."""

import asyncio
from typing import Optional
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn

app = typer.Typer(
    name="sra",
    help="CLI do Smart Research Agent v6.0",
    add_completion=False,
)
console = Console()


@app.command()
def search(
    query: str = typer.Argument(..., help="Termo principal a pesquisar"),
    mode: str = typer.Option(
        "cirurgia", "--mode", "-m", help="Preset de modo de operacao"
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Caminho do arquivo Markdown para escrita"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Imprime output formatado em JSON puro"
    ),
):
    """Executa o pipeline completo de pesquisa a partir do terminal."""
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[cyan]Executando pesquisa: {query[:50]}...", total=None
        )

        try:
            config = Config()
            config.operation_mode = mode
            orchestrator = create_orchestrator(config)

            # Executa com asyncio local
            result = asyncio.run(orchestrator.research(query))
            progress.remove_task(task)
        except Exception as e:
            progress.remove_task(task)
            console.print(f"[bold red]Erro durante execução: {e}[/bold red]")
            raise typer.Exit(1)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(result if isinstance(result, str) else str(result))
        console.print(f"[bold green]Resultado salvo em: {output}[/bold green]")
    elif json_output:
        import json

        console.print_json(json.dumps({"query": query, "result": result}))
    else:
        console.print(Markdown(result if isinstance(result, str) else str(result)))


@app.command("schedule")
def schedule_research(
    query: str = typer.Argument(..., help="Query de pesquisa recorrente"),
    cron: str = typer.Option(
        "0 8 * * *", "--cron", "-c", help="Expressão cron (padrão: diário às 8h)"
    ),
    webhook_url: Optional[str] = typer.Option(
        None, "--webhook", "-w", help="URL para alertas de mudança (Slack/Discord/N8N)"
    ),
    output_dir: str = typer.Option(
        "reports/scheduled", "--output-dir", "-o", help="Diretório para os relatórios"
    ),
    no_alerts: bool = typer.Option(
        False, "--no-alerts", help="Desabilita alertas de mudança para este job"
    ),
):
    """Agenda uma pesquisa recorrente com detecção de mudanças e alertas via webhook."""
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator
    from src.scheduler import ResearchScheduler

    orchestrator = create_orchestrator(Config())
    scheduler = ResearchScheduler(orchestrator)
    job_id = scheduler.schedule_research(
        query=query,
        cron_expr=cron,
        output_dir=output_dir,
        webhook_url=webhook_url,
        alert_on_changes=not no_alerts,
    )
    console.print(f"[bold green]✅ Pesquisa agendada:[/bold green] {job_id}")
    console.print(f"  query='{query}' | cron='{cron}' | output='{output_dir}'")


@app.command("schedule-list")
def schedule_list():
    """Lista todas as pesquisas recorrentes agendadas."""
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator
    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(create_orchestrator(Config()))
    jobs = scheduler.list_jobs()
    if not jobs:
        console.print("  Nenhuma pesquisa agendada.")
        return
    for job in jobs:
        console.print(
            f"  * [cyan]{job['id']}[/cyan] — '{job['query']}' | cron='{job['cron']}' "
            f"| last_run={job.get('last_run') or 'nunca'}"
        )


@app.command("schedule-run")
def schedule_run(
    job_id: str = typer.Argument(..., help="ID do job a executar imediatamente"),
):
    """Executa imediatamente um job agendado (útil para testar/forçar execução)."""
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator
    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(create_orchestrator(Config()))
    try:
        asyncio.run(scheduler.run_scheduled_research(job_id))
        console.print(f"[bold green]✅ Job '{job_id}' executado.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Erro ao executar job '{job_id}': {e}[/bold red]")
        raise typer.Exit(1)


@app.command("schedule-cancel")
def schedule_cancel(
    job_id: str = typer.Argument(..., help="ID do job a cancelar"),
):
    """Cancela e remove uma pesquisa recorrente agendada."""
    from src.config import Config
    from src.orchestrator_factory import create_orchestrator
    from src.scheduler import ResearchScheduler

    scheduler = ResearchScheduler(create_orchestrator(Config()))
    if scheduler.cancel_job(job_id):
        console.print(f"[bold green]✅ Job '{job_id}' cancelado.[/bold green]")
    else:
        console.print(f"[yellow]Job '{job_id}' não encontrado.[/yellow]")
        raise typer.Exit(1)


@app.command()
def status():
    """Consulta o status atual de todos os disjuntores da aplicação."""
    from src.utils.circuit_breaker import CircuitBreakerRegistry

    console.print("[bold cyan]Status dos Circuit Breakers:[/bold cyan]")
    statuses = CircuitBreakerRegistry.status_all()
    if not statuses:
        console.print("  Nenhum disjuntor ativo no momento.")
        return

    for name, state in statuses.items():
        color = "green" if state == "closed" else "red" if state == "open" else "yellow"
        console.print(f"  * {name}: [{color}]{state.upper()}[/{color}]")


if __name__ == "__main__":
    app()
