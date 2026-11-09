"""Interfaz de línea de comandos."""

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich import box
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
from odds_scanner.normalize import normalize_events
from odds_scanner.rank import ARBITRAGE_WARNING, RankFilters, rank, summarize_markets

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


# --- rank (fase 4) ------------------------------------------------------------


def _load_fixture_events(path: Path) -> list[Event]:
    if not path.is_file():
        raise _fail(f"{path} no existe")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Event.model_validate(item) for item in raw]
    except (ValueError, TypeError) as exc:
        raise _fail(f"{path} no es una fixture de cuotas válida: {type(exc).__name__}") from None


def _split(value: str | None) -> set[str] | None:
    return {v.strip() for v in value.split(",") if v.strip()} if value else None


@app.command(name="rank")
def rank_cmd(
    from_fixture: Annotated[
        Path, typer.Option("--from-fixture", help="JSON grabado con --record (no gasta cuota).")
    ],
    sport: Annotated[str | None, typer.Option(help="Deportes separados por coma.")] = None,
    market: Annotated[str | None, typer.Option(help="Mercados separados por coma.")] = None,
    within_hours: Annotated[
        float | None, typer.Option(help="Solo eventos que empiezan dentro de N horas.")
    ] = None,
    min_odds: Annotated[float | None, typer.Option(help="Mejor cuota mínima.")] = None,
    max_odds: Annotated[float | None, typer.Option(help="Mejor cuota máxima.")] = None,
    top: Annotated[int, typer.Option(min=1, help="Filas a mostrar.")] = 15,
) -> None:
    """Rankea resultados por margen combinado (menor = mejor) desde una fixture."""
    console = _console()
    events = _load_fixture_events(from_fixture)
    try:
        filters = RankFilters(
            sports=_split(sport),
            markets=_split(market),
            within_hours=within_hours,
            min_odds=min_odds,
            max_odds=max_odds,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from None
    normalized = normalize_events(events)
    summaries = summarize_markets(normalized.rows)
    ranked = rank(summaries, filters, now=datetime.now(UTC))
    reasons = ", ".join(f"{k}={v}" for k, v in sorted(normalized.discarded.items()))
    console.print(
        f"Eventos: {len(events)} | filas válidas: {len(normalized.rows)} | "
        f"mercados: {len(summaries)} | resultados tras filtros: {len(ranked)}"
    )
    console.print(
        f"Descartadas: {normalized.discarded_total}" + (f" ({reasons})" if reasons else "")
    )

    shown = ranked[:top]
    table = Table(
        title=f"Top {len(shown)} por margen combinado",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
    )
    # (nombre, ancho mínimo, ancho máximo, alineación): suma 119 columnas con separadores.
    columns: list[tuple[str, int, int, Literal["left", "right"]]] = [
        ("#", 2, 3, "right"),
        ("inicio CL", 11, 11, "left"),
        ("evento", 10, 22, "left"),
        ("mercado", 7, 8, "left"),
        ("resultado", 8, 15, "left"),
        ("cuota", 5, 6, "right"),
        ("casa", 8, 12, "left"),
        ("m. casa", 7, 7, "right"),
        ("m. comb.", 11, 11, "right"),
    ]
    for name, min_w, max_w, justify in columns:
        table.add_column(
            name,
            min_width=min_w,
            max_width=max_w,
            no_wrap=True,
            overflow="ellipsis",
            justify=justify,
        )
    for i, r in enumerate(shown, start=1):
        market_label = r.market if r.line is None else f"{r.market} {r.line:g}"
        table.add_row(
            str(i),
            r.commence_time_cl.strftime("%d-%m %H:%M"),
            f"{r.home} - {r.away}",
            market_label,
            r.outcome if r.point is None else f"{r.outcome} {r.point:+g}",
            f"{r.best_price:.2f}",
            r.bookmaker,
            f"{r.book_margin:+.2%}",
            f"{r.best_market_margin:+.2%}" + (" ARB" if r.is_arbitrage else "    "),
        )
    console.print(table)
    if any(r.is_arbitrage for r in shown):
        console.print(f"ADVERTENCIA (ARB): {ARBITRAGE_WARNING}")
