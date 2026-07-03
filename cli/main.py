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
    mode: str = typer.Option("cirurgia", "--mode", "-m", help="Preset de modo de operacao"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Caminho do arquivo Markdown para escrita"),
    json_output: bool = typer.Option(False, "--json", help="Imprime output formatado em JSON puro")
):
    """Executa o pipeline completo de pesquisa a partir do terminal."""
    from src.config import Config
    from src.orchestrator import Orchestrator

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"[cyan]Executando pesquisa: {query[:50]}...", total=None)

        try:
            config = Config()
            config.operation_mode = mode
            orchestrator = Orchestrator(config)
            
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