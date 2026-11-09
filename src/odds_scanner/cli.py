"""Interfaz de línea de comandos."""

from collections import defaultdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from odds_scanner import __version__
from odds_scanner.client import (
    Event,
    OddsApiError,
    OddsClient,
    QuotaStatus,
    estimate_cost,
    read_quota,
)
from odds_scanner.config import DEFAULT_ENV_FILE, ConfigError, load_settings
from odds_scanner.doctor import run_checks
from odds_scanner.math import best_market_margin

DATA_DIR = Path("data")
FIXTURES_DIR = Path("fixtures")

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


# --- comandos con red (fase 3) ------------------------------------------------


def _console() -> Console:
    return Console(width=120, highlight=False)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"ERROR: {message}")
    return typer.Exit(code=1)


def _make_client() -> OddsClient:
    try:
        settings = load_settings()
        key = settings.require_api_key()
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    return OddsClient(
        key,
        data_dir=DATA_DIR,
        fixtures_dir=FIXTURES_DIR,
        min_quota_reserve=settings.min_quota_reserve,
    )


def _quota_line(label: str, quota: QuotaStatus | None) -> str:
    if quota is None:
        return f"{label}: desconocida"
    return (
        f"{label}: restantes={quota.remaining} usadas={quota.used} "
        f"última={quota.last} ({quota.updated_at})"
    )


def _h2h_best_margin(event: Event) -> float | None:
    """Margen del mejor mercado h2h entre casas (sin mercados lay)."""
    offers: dict[str, list[float]] = defaultdict(list)
    for bookmaker in event.bookmakers:
        for market in bookmaker.markets:
            if market.key == "h2h":
                for outcome in market.outcomes:
                    offers[outcome.name].append(outcome.price)
    if len(offers) < 2:
        return None
    return best_market_margin(list(offers.values()))


@app.command()
def sports(
    include_all: Annotated[bool, typer.Option("--all", help="Incluye fuera de temporada.")] = False,
) -> None:
    """Lista los deportes activos (gratis) y muestra el consumo de cuota."""
    console = _console()
    with _make_client() as client:
        console.print(_quota_line("Cuota antes", client.quota()))
        try:
            result = client.list_sports(include_all=include_all)
        except OddsApiError as exc:
            raise _fail(str(exc)) from None
        table = Table(title=f"Deportes ({len(result)})")
        for col in ("key", "grupo", "título", "outrights"):
            table.add_column(col)
        for sport in result:
            table.add_row(sport.key, sport.group, sport.title, "sí" if sport.has_outrights else "")
        console.print(table)
        origin = " (desde caché, sin consumo)" if client.last_from_cache else ""
        console.print(_quota_line("Cuota después" + origin, client.quota()))


@app.command()
def fetch(
    sport_key: Annotated[str, typer.Argument(help="Clave del deporte, p. ej. soccer_epl.")],
    regions: Annotated[str, typer.Option(help="Regiones separadas por coma.")] = "eu",
    markets: Annotated[str, typer.Option(help="Mercados separados por coma.")] = "h2h",
    record: Annotated[
        bool, typer.Option("--record", help="Graba la respuesta cruda en fixtures/.")
    ] = False,
) -> None:
    """Descarga cuotas de un deporte respetando la reserva de cuota."""
    console = _console()
    region_list, market_list = regions.split(","), markets.split(",")
    try:
        cost = estimate_cost(region_list, market_list)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    with _make_client() as client:
        console.print(_quota_line("Cuota antes", client.quota()))
        console.print(f"Costo estimado: {cost} (reserva mínima {client.min_quota_reserve})")
        try:
            events = client.get_odds(sport_key, region_list, market_list, record=record)
        except OddsApiError as exc:
            raise _fail(str(exc)) from None
        table = Table(title=f"{sport_key}: {len(events)} eventos")
        for col in ("inicio (UTC)", "partido", "casas", "margen mejor h2h"):
            table.add_column(col)
        for event in events:
            margin = _h2h_best_margin(event)
            table.add_row(
                event.commence_time.strftime("%Y-%m-%d %H:%M"),
                f"{event.home_team} vs {event.away_team}",
                str(len(event.bookmakers)),
                "-" if margin is None else f"{margin:+.2%}",
            )
        console.print(table)
        if client.last_from_cache:
            console.print("Respuesta desde caché (TTL 10 min): no consumió cuota.")
        if client.last_record_path is not None:
            console.print(f"Grabado: {client.last_record_path}")
        console.print(_quota_line("Cuota después", read_quota(client.data_dir)))
