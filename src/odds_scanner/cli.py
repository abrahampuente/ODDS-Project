"""Interfaz de línea de comandos."""

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from odds_scanner import __version__, timeutil
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
from odds_scanner.export import build_sheets, export_csv, export_xlsx
from odds_scanner.ledger import DEFAULT_DB, Ledger
from odds_scanner.ledger_cli import LEDGER_COMMANDS, ledger_app
from odds_scanner.math import best_market_margin
from odds_scanner.normalize import normalize_events
from odds_scanner.rank import (
    ARBITRAGE_WARNING,
    RankedOutcome,
    RankFilters,
    rank,
    summarize_markets,
)
from odds_scanner.report import build_report, format_report
from odds_scanner.store import (
    closing_best_price,
    latest_capture,
    list_captures,
    load_last_rank,
    save_capture,
    save_last_rank,
)
from odds_scanner.value import EV_DISCLAIMER, INSUFFICIENT_DATA, MIN_BOOKS

DATA_DIR = Path("data")
FIXTURES_DIR = Path("fixtures")

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Escáner de cuotas.")
app.add_typer(ledger_app, name="ledger")


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


# --- rank / value / scan (fases 4, 5 y 7) -------------------------------------


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


def _split_list(value: str) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))


class RankView(StrEnum):
    """Vistas del ranking."""

    margin = "margin"
    value = "value"


# (encabezado, ancho mínimo, ancho máximo, alineación). Con box=None y padding 1
# colapsado (1 espacio entre columnas), la tabla ocupa como máximo 120 columnas.
RANK_COLUMNS: list[tuple[str, int, int, Literal["left", "right"]]] = [
    ("#", 2, 2, "right"),
    ("inicio CL", 11, 11, "left"),
    ("evento", 6, 21, "left"),
    ("merc", 4, 6, "left"),
    ("resultado", 6, 14, "left"),
    ("cuota", 5, 5, "right"),
    ("casa", 4, 11, "left"),
    ("m.casa", 6, 6, "right"),
    ("m.comb", 10, 10, "right"),
    ("n", 2, 2, "right"),
    ("p.justa", 7, 7, "right"),
    ("EV", 7, 7, "right"),
    ("etiqueta", 8, 8, "left"),
]


def _rank_table(title: str, rows: Sequence[RankedOutcome]) -> Table:
    table = Table(title=title, box=None, pad_edge=False, padding=(0, 1), collapse_padding=True)
    for name, min_w, max_w, justify in RANK_COLUMNS:
        table.add_column(
            name,
            min_width=min_w,
            max_width=max_w,
            no_wrap=True,
            overflow="ellipsis",
            justify=justify,
            header_style="bold",
        )
    for i, r in enumerate(rows, start=1):
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
            str(r.n_books),
            "-" if r.fair_prob is None else f"{r.fair_prob:.4f}",
            "-" if r.ev is None else f"{r.ev:+.2%}",
            "insuf." if r.ev is None else r.label,
        )
    return table


def _filters(
    sport: str | None,
    market: str | None,
    hours: float | None,
    odds_min: float | None,
    odds_max: float | None,
) -> RankFilters:
    try:
        return RankFilters(
            sports=_split(sport),
            markets=_split(market),
            within_hours=hours,
            min_odds=odds_min,
            max_odds=odds_max,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from None


def _show_ranking(
    console: Console,
    events: Sequence[Event],
    *,
    source: str,
    filters: RankFilters,
    view: RankView,
    top: int,
) -> None:
    """Normaliza, rankea, imprime y guarda el ranking (base de `bet --row N`)."""
    normalized = normalize_events(events)
    summaries = summarize_markets(normalized.rows)
    now = timeutil.utcnow()
    all_rows = rank(summaries, filters, now=now)
    ranked = (
        all_rows if view is RankView.margin else rank(summaries, filters, now=now, view="value")
    )
    reasons = ", ".join(f"{k}={v}" for k, v in sorted(normalized.discarded.items()))
    console.print(f"Fuente: {source}")
    console.print(
        f"Eventos: {len(events)} | filas válidas: {len(normalized.rows)} | "
        f"mercados: {len(summaries)} | resultados tras filtros: {len(all_rows)}"
    )
    console.print(
        f"Descartadas: {normalized.discarded_total}" + (f" ({reasons})" if reasons else "")
    )
    without_ev = sum(1 for r in all_rows if r.ev is None)
    if without_ev:
        console.print(
            f"Resultados sin EV ({INSUFFICIENT_DATA}, menos de {MIN_BOOKS} casas): {without_ev}"
        )
    shown = ranked[:top]
    title = (
        f"Top {len(shown)} por margen combinado"
        if view is RankView.margin
        else f"Top {len(shown)} por EV (consenso sin margen, >= {MIN_BOOKS} casas)"
    )
    console.print(_rank_table(title, shown))
    save_last_rank(DATA_DIR, ranked, view=view.value, source=source, created_at=now)
    console.print("Para registrar una fila: `bet --row N --pot P --stake S`.")
    sharp_books = sorted({r.sharp_book for r in shown if r.sharp_book})
    if sharp_books:
        console.print(f"Referencia sharp disponible: {', '.join(sharp_books)}")
    if any(r.is_arbitrage for r in shown):
        console.print(f"ADVERTENCIA (ARB): {ARBITRAGE_WARNING}")
    console.print(f"ADVERTENCIA (EV): {EV_DISCLAIMER}")


def _ranking_source(from_fixture: Path | None) -> tuple[list[Event], str]:
    if from_fixture is not None:
        return _load_fixture_events(from_fixture), f"fixture {from_fixture}"
    capture = latest_capture(DATA_DIR)
    if capture is None:
        raise _fail("no hay capturas: ejecuta `scan` o usa --from-fixture ARCHIVO")
    when = capture.captured_at.strftime("%Y-%m-%d %H:%M")
    return capture.events, f"captura {capture.path.name} ({when} UTC)"


FromFixture = Annotated[
    Path | None,
    typer.Option("--from-fixture", help="JSON grabado con --record; sin él usa la última captura."),
]
SportOpt = Annotated[str | None, typer.Option("--sport", help="Deportes separados por coma.")]
MarketOpt = Annotated[str | None, typer.Option("--market", help="Mercados separados por coma.")]
HoursOpt = Annotated[
    float | None,
    typer.Option("--hours", "--within-hours", help="Solo eventos que empiezan dentro de N horas."),
]
OddsMinOpt = Annotated[
    float | None, typer.Option("--odds-min", "--min-odds", help="Mejor cuota mínima.")
]
OddsMaxOpt = Annotated[
    float | None, typer.Option("--odds-max", "--max-odds", help="Mejor cuota máxima.")
]
TopOpt = Annotated[int, typer.Option(min=1, help="Filas a mostrar.")]


@app.command(name="rank")
def rank_cmd(
    from_fixture: FromFixture = None,
    view: Annotated[
        RankView, typer.Option(help="margin: margen combinado asc. | value: EV desc.")
    ] = RankView.margin,
    sport: SportOpt = None,
    market: MarketOpt = None,
    hours: HoursOpt = None,
    odds_min: OddsMinOpt = None,
    odds_max: OddsMaxOpt = None,
    top: TopOpt = 15,
) -> None:
    """Rankea resultados por margen combinado (o EV) desde la última captura o una fixture."""
    events, source = _ranking_source(from_fixture)
    filters = _filters(sport, market, hours, odds_min, odds_max)
    _show_ranking(_console(), events, source=source, filters=filters, view=view, top=top)


@app.command(name="value")
def value_cmd(
    from_fixture: FromFixture = None,
    sport: SportOpt = None,
    market: MarketOpt = None,
    hours: HoursOpt = None,
    odds_min: OddsMinOpt = None,
    odds_max: OddsMaxOpt = None,
    top: TopOpt = 15,
) -> None:
    """Vista value: resultados ordenados por EV estimado (consenso de mercado sin margen)."""
    events, source = _ranking_source(from_fixture)
    filters = _filters(sport, market, hours, odds_min, odds_max)
    _show_ranking(_console(), events, source=source, filters=filters, view=RankView.value, top=top)


@app.command()
def scan(
    sports_csv: Annotated[
        str, typer.Option("--sports", help="Deportes separados por coma, p. ej. soccer_epl.")
    ],
    markets: Annotated[str, typer.Option(help="Mercados separados por coma.")] = "h2h",
    regions: Annotated[str, typer.Option(help="Regiones separadas por coma.")] = "eu",
    hours: HoursOpt = None,
    odds_min: OddsMinOpt = None,
    odds_max: OddsMaxOpt = None,
    view: Annotated[RankView, typer.Option(help="Vista del ranking.")] = RankView.margin,
    top: TopOpt = 15,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Solo muestra el costo; no llama a la API.")
    ] = False,
) -> None:
    """Captura cuotas de varios deportes y las rankea.

    Muestra el costo estimado en consultas y pide confirmación (y/N) antes de llamar a
    la API; --yes la omite. Respeta la reserva mínima de cuota.
    """
    console = _console()
    sport_list = _split_list(sports_csv)
    region_list, market_list = _split_list(regions), _split_list(markets)
    if not sport_list:
        raise _fail("indica al menos un deporte en --sports")
    try:
        per_sport = estimate_cost(region_list, market_list)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    total = per_sport * len(sport_list)
    reserve = load_settings().min_quota_reserve
    console.print(
        f"Costo estimado: {total} consultas ({len(sport_list)} deportes x {len(market_list)} "
        f"mercados x {len(region_list)} regiones; 0 si no hay eventos)"
    )
    for sport_key in sport_list:
        console.print(f"  {sport_key}: {per_sport}")
    quota = read_quota(DATA_DIR)
    if quota is None:
        console.print("Cuota: desconocida (ejecuta `odds-scanner sports`, gratis, para leerla)")
    else:
        console.print(
            f"Cuota: restantes {quota.remaining}, quedarían {quota.remaining - total} "
            f"(reserva mínima {reserve})"
        )
    if dry_run:
        console.print("dry-run: no se llamó a la API ni se gastó cuota.")
        return
    if quota is None:
        raise _fail("cuota desconocida: ejecuta `odds-scanner sports` (gratis) antes de scan")
    if quota.remaining - total < reserve:
        raise _fail(
            f"bloqueado: quedarían {quota.remaining - total} consultas, bajo la reserva {reserve}"
        )
    if not yes and not typer.confirm(f"¿Gastar hasta {total} consultas?", default=False):
        typer.echo("Cancelado: no se llamó a la API.")
        raise typer.Exit(code=1)

    events: list[Event] = []
    with _make_client() as client:
        for sport_key in sport_list:
            try:
                events += client.get_odds(sport_key, region_list, market_list)
            except OddsApiError as exc:
                raise _fail(f"{sport_key}: {exc}") from None
        console.print(_quota_line("Cuota después", client.quota()))
    path = save_capture(
        DATA_DIR,
        events,
        sports=sport_list,
        markets=market_list,
        regions=region_list,
        captured_at=timeutil.utcnow(),
    )
    console.print(f"Captura guardada: {path}")
    filters = _filters(None, None, hours, odds_min, odds_max)
    _show_ranking(
        console, events, source=f"captura {path.name}", filters=filters, view=view, top=top
    )


# --- export / report (fase 7) -------------------------------------------------


class ExportFormat(StrEnum):
    """Formatos de exportación."""

    csv = "csv"
    xlsx = "xlsx"


@app.command(name="export")
def export_cmd(
    fmt: Annotated[ExportFormat, typer.Option("--format", help="csv o xlsx.")],
    out: Annotated[
        Path | None,
        typer.Option(help="Archivo .xlsx o carpeta para los CSV (por defecto data/exports/)."),
    ] = None,
    db: Annotated[Path, typer.Option(help="Base del ledger.")] = DEFAULT_DB,
) -> None:
    """Exporta el último ranking, las apuestas y los pozos (hojas ranking, apuestas, pozos)."""
    console = _console()
    last = load_last_rank(DATA_DIR)
    if last is None:
        console.print("Aviso: sin ranking guardado; la hoja ranking queda vacía.")
    with Ledger(db) as ledger:
        sheets = build_sheets(last.rows if last else [], ledger.bets(), ledger.pots())
    stamp = timeutil.utcnow().strftime("%Y%m%dT%H%M%SZ")
    if fmt is ExportFormat.xlsx:
        target = out or DATA_DIR / "exports" / f"export_{stamp}.xlsx"
        paths = [export_xlsx(target, sheets)]
    else:
        paths = export_csv(out or DATA_DIR / "exports" / f"export_{stamp}", sheets)
    for name, (_, rows) in sheets.items():
        console.print(f"  {name}: {len(rows)} filas")
    for path in paths:
        console.print(f"Exportado: {path}")


@app.command()
def report(db: Annotated[Path, typer.Option(help="Base del ledger.")] = DEFAULT_DB) -> None:
    """Resumen en texto: saldos, distancia a 500.000, ganadas/perdidas, ROI, margen y CLV."""
    with Ledger(db) as ledger:
        state, bets = ledger.state(), ledger.bets()
    captures = list_captures(DATA_DIR)
    closing: dict[int, float] = {}
    for b in bets:
        if b.event_id is None or b.commence_time is None:
            continue
        price = closing_best_price(
            captures,
            event_id=b.event_id,
            market=b.market,
            line=b.line,
            selection=b.selection,
            commence_time=datetime.fromisoformat(b.commence_time),
        )
        if price is not None:
            closing[b.id] = price
    typer.echo(format_report(build_report(state, bets, closing)))


for _name, _command in LEDGER_COMMANDS.items():
    app.command(name=_name)(_command)
