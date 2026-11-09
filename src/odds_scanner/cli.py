"""Interfaz de línea de comandos."""

from pathlib import Path
from typing import Annotated

import typer

from odds_scanner import __version__
from odds_scanner.config import DEFAULT_ENV_FILE
from odds_scanner.doctor import run_checks

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Escáner de cuotas.")


@app.command()
def version() -> None:
    """Muestra la versión instalada."""
    typer.echo(f"odds-scanner {__version__}")


@app.command()
def doctor(
    env_file: Annotated[Path, typer.Option(help="Ruta del archivo .env.")] = DEFAULT_ENV_FILE,
    data_dir: Annotated[Path, typer.Option(help="Directorio de datos.")] = Path("data"),
) -> None:
    """Verifica que el entorno esté listo. Sale con código 1 si algo falla."""
    results = run_checks(env_file=env_file, data_dir=data_dir)
    for r in results:
        typer.echo(f"[{'OK' if r.ok else 'FALLA'}] {r.name}: {r.detail}")
    failed = [r for r in results if not r.ok]
    if failed:
        typer.echo(f"\n{len(failed)} chequeo(s) fallaron.")
        raise typer.Exit(code=1)
    typer.echo("\nTodo en orden.")
