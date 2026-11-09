"""Comandos del ledger (I/O de consola sobre ledger.py).

Se registran en el grupo `odds-scanner ledger ...` y también en el nivel superior
(`odds-scanner bet`, `settle`, `merge`, `plan`, `status`, `init`, `log`).
"""

from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from odds_scanner import timeutil
from odds_scanner.ledger import (
    DEFAULT_DB,
    GOAL,
    IN_PROGRESS,
    Ledger,
    LedgerError,
    LedgerState,
    payout_for,
)
from odds_scanner.math import margin
from odds_scanner.store import DEFAULT_DATA_DIR, load_last_rank

MAX_MANUAL_MARGIN = 0.07
RANK_MAX_AGE = timedelta(minutes=30)

ledger_app = typer.Typer(
    no_args_is_help=True,
    help="Registro de los 4 pozos (solo registra; nunca coloca apuestas reales).",
)

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Ruta de la base SQLite (por defecto data/ledger.db).")
]


class SettleResult(StrEnum):
    """Resultados válidos para liquidar."""

    won = "won"
    lost = "lost"
    void = "void"


def clp(amount: int) -> str:
    """Formatea pesos chilenos: 112500 -> '112.500'."""
    return f"{amount:,}".replace(",", ".")


def _console() -> Console:
    return Console(width=120, highlight=False)


def _db(ctx: typer.Context, db: Path | None) -> Path:
    """--db del comando > --db del grupo `ledger` > data/ledger.db."""
    if db is not None:
        return db
    group_db = ctx.obj.get("db") if isinstance(ctx.obj, dict) else None
    if isinstance(group_db, Path):
        return group_db
    return DEFAULT_DB


def _fail(message: str) -> typer.Exit:
    typer.echo(f"ERROR: {message}")
    return typer.Exit(code=1)


def _cancel() -> typer.Exit:
    typer.echo("Cancelado: no se registró nada.")
    return typer.Exit(code=1)


def _print_state(console: Console, state: LedgerState) -> None:
    progress = state.total_balance / GOAL
    console.print(
        f"Estado: {state.status} | saldo total: {clp(state.total_balance)} / meta {clp(GOAL)}"
        f" ({progress:.1%}) | en juego (pendiente): {clp(state.pending_stake)}"
    )
    if not state.pots:
        return
    table = Table(title="Pozos")
    for col in ("pozo", "inicial", "saldo", "estado"):
        table.add_column(col, justify="right" if col != "estado" else "left")
    for pot in state.pots:
        table.add_row(str(pot.id), clp(pot.initial), clp(pot.balance), pot.status)
    console.print(table)


@ledger_app.callback()
def ledger_main(
    ctx: typer.Context,
    db: Annotated[Path, typer.Option(help="Ruta de la base SQLite.")] = DEFAULT_DB,
) -> None:
    """Registro de pozos en SQLite."""
    ctx.obj = {"db": db}


@ledger_app.command()
def init(
    ctx: typer.Context,
    reset: Annotated[bool, typer.Option("--reset", help="Reinicia pozos y apuestas.")] = False,
    confirm: Annotated[bool, typer.Option("--confirm", help="Confirma el reinicio.")] = False,
    db: DbOption = None,
) -> None:
    """Crea los 4 pozos de 25.000 (solo una vez; reiniciar exige --reset --confirm)."""
    with Ledger(_db(ctx, db)) as ledger:
        try:
            ledger.init(reset=reset, confirm=confirm)
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        console.print(f"Pozos creados en {ledger.path}" + (" (reinicio)" if reset else ""))
        _print_state(console, ledger.state())


def _parse_market_odds(raw: str, odds: float) -> float:
    try:
        prices = [float(p) for p in raw.split(",") if p.strip()]
        result = margin(prices)
    except ValueError:
        raise _fail("--market-odds debe ser una lista de cuotas > 1.0 separadas por coma") from None
    if not any(abs(p - odds) < 1e-9 for p in prices):
        raise _fail("--market-odds debe incluir la cuota ingresada en --odds")
    return result


@ledger_app.command()
def bet(
    ctx: typer.Context,
    pot: Annotated[int, typer.Option(help="Pozo (1-4).")],
    stake: Annotated[int, typer.Option(help="Monto en CLP (entero).")],
    row: Annotated[
        int | None, typer.Option(help="Fila N del último ranking (rank/value/scan).")
    ] = None,
    odds: Annotated[float | None, typer.Option(help="Cuota decimal (entrada manual).")] = None,
    event: Annotated[str | None, typer.Option(help="Evento (entrada manual).")] = None,
    market: Annotated[str | None, typer.Option(help="Mercado (entrada manual).")] = None,
    selection: Annotated[str | None, typer.Option(help="Resultado (entrada manual).")] = None,
    bookmaker: Annotated[str | None, typer.Option(help="Casa (entrada manual).")] = None,
    market_odds: Annotated[
        str | None,
        typer.Option(help="Todas las cuotas del mercado en esa casa, p. ej. 2.10,3.40,3.60."),
    ] = None,
    notes: Annotated[str, typer.Option(help="Notas.")] = "",
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
    accept_high_margin: Annotated[
        bool,
        typer.Option("--accept-high-margin", help="Con --yes, acepta margen > 7% o desconocido."),
    ] = False,
    db: DbOption = None,
    data_dir: Annotated[Path, typer.Option(help="Directorio de datos.")] = DEFAULT_DATA_DIR,
) -> None:
    """Registra una apuesta hecha por ti (no la coloca).

    Debe salir de una fila del ranking (--row N). Si la ingresas a mano, se pide una
    confirmación extra cuando el margen de la casa supera 7% o es desconocido. Antes de
    registrar muestra pozo, stake, cuota, ganancia posible y la cuota necesaria según plan.
    """
    console = _console()
    manual = {
        "--odds": odds,
        "--event": event,
        "--market": market,
        "--selection": selection,
        "--bookmaker": bookmaker,
    }
    source: Literal["manual", "row"]
    book_margin: float | None
    ev: float | None = None
    event_id: str | None = None
    commence_time: str | None = None
    line: float | None = None
    fair_prob: float | None = None
    if row is not None:
        if any(v is not None for v in manual.values()) or market_odds is not None:
            raise _fail("usa --row o los datos manuales (--odds, --event, ...), no ambos")
        last = load_last_rank(data_dir)
        if last is None:
            raise _fail("no hay ranking guardado: ejecuta `rank`, `value` o `scan` antes de --row")
        if not 1 <= row <= len(last.rows):
            raise _fail(f"la fila {row} no existe en el último ranking ({len(last.rows)} filas)")
        r = last.rows[row - 1]
        now = timeutil.utcnow()
        if r.commence_time_utc <= now:
            raise _fail(f"el evento ya empezó ({r.commence_time_utc:%Y-%m-%d %H:%M} UTC)")
        age = now - last.created_at
        if age > RANK_MAX_AGE:
            console.print(
                f"ADVERTENCIA: el ranking tiene {int(age.total_seconds() // 60)} min; "
                "confirma que la cuota sigue disponible."
            )
        odds, event, market = r.best_price, f"{r.home} vs {r.away}", r.market
        selection, bookmaker = r.outcome, r.bookmaker
        book_margin, ev, source = r.book_margin, r.ev, "row"
        event_id, commence_time = r.event_id, r.commence_time_utc.isoformat()
        line, fair_prob = r.line, r.fair_prob
    else:
        missing = [flag for flag, value in manual.items() if value is None]
        if missing:
            raise _fail(f"faltan {', '.join(missing)} (o usa --row N con una fila del ranking)")
        assert odds is not None
        book_margin = _parse_market_odds(market_odds, odds) if market_odds else None
        source = "manual"
    assert odds is not None and event and market and selection and bookmaker

    with Ledger(_db(ctx, db)) as ledger:
        try:
            pot_info = ledger.check_bet(pot, stake, odds)
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        state, plan = ledger.state(), ledger.plan()
        payout = payout_for(stake, odds)
        needed = (GOAL - (state.total_balance - stake)) / stake
        plan_entry = next((p for p in plan.per_pot if p.pot_id == pot), None)

        console.print("Resumen de la apuesta (odds-scanner solo registra; no coloca apuestas):")
        console.print(f"  Pozo: {pot} (saldo {clp(pot_info.balance)})")
        console.print(f"  Stake: {clp(stake)}")
        console.print(
            f"  Cuota: {odds:.2f} en {bookmaker} ({'fila del ranking' if row else 'manual'})"
        )
        console.print(f"  Selección: {selection} | {event} | {market}")
        console.print(f"  Pago si gana: {clp(payout)} | Ganancia posible: {clp(payout - stake)}")
        console.print(
            "  Margen de la casa: "
            + ("desconocido" if book_margin is None else f"{book_margin:.2%}")
        )
        if ev is not None:
            console.print(f"  EV estimado (consenso): {ev:+.2%}")
        if plan_entry is not None:
            console.print(
                f"  Cuota necesaria según plan (pozo {pot}): {plan_entry.odds:.2f} "
                f"(todo el saldo del pozo a una apuesta)"
            )
        console.print(
            f"  Con este stake, para llegar a {clp(GOAL)} si gana necesitas cuota >= {needed:.2f}"
        )
        console.print(f"  ¿Llega a {clp(GOAL)} si gana?: {'sí' if odds >= needed else 'no'}")

        if source == "manual" and (book_margin is None or book_margin > MAX_MANUAL_MARGIN):
            reason = (
                "margen desconocido (no se entregó --market-odds)"
                if book_margin is None
                else f"margen de la casa {book_margin:.2%} > 7%"
            )
            console.print(f"ADVERTENCIA: cuota ingresada a mano con {reason}.")
            if yes and not accept_high_margin:
                raise _fail("con --yes debes agregar --accept-high-margin para registrarla igual")
            if not yes and not typer.confirm(
                "¿Continuar con esta cuota igualmente?", default=False
            ):
                raise _cancel()
        if not yes and not typer.confirm("¿Registrar esta apuesta?", default=False):
            raise _cancel()

        try:
            placed = ledger.place_bet(
                pot_id=pot,
                stake=stake,
                odds=odds,
                event=event,
                market=market,
                selection=selection,
                bookmaker=bookmaker,
                notes=notes,
                book_margin=book_margin,
                ev=ev,
                source=source,
                event_id=event_id,
                commence_time=commence_time,
                line=line,
                fair_prob=fair_prob,
            )
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console.print(
            f"Apuesta #{placed.id} registrada: pozo {pot}, {clp(stake)} a {odds:.2f}. "
            f"Saldo del pozo {pot}: {clp(ledger.pot(pot).balance)}"
        )


@ledger_app.command()
def settle(
    ctx: typer.Context,
    bet_id: Annotated[int, typer.Argument(help="Id de la apuesta.")],
    result: Annotated[SettleResult, typer.Argument(help="won, lost o void.")],
    db: DbOption = None,
) -> None:
    """Liquida una apuesta pendiente: won suma stake x cuota, lost nada, void devuelve."""
    with Ledger(_db(ctx, db)) as ledger:
        try:
            settled = ledger.settle(bet_id, result.value)
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        [pot_id] = settled.pot_ids
        pot = ledger.pot(pot_id)
        console.print(
            f"Apuesta #{settled.id} liquidada: {settled.status}, pago {clp(settled.payout or 0)}."
            f" Pozo {pot_id}: saldo {clp(pot.balance)} ({pot.status})."
        )
        _print_state(console, ledger.state())


@ledger_app.command()
def merge(
    ctx: typer.Context,
    into: Annotated[int, typer.Argument(help="Pozo destino.")],
    sources: Annotated[list[int] | None, typer.Argument(help="Pozos de origen.")] = None,
    db: DbOption = None,
) -> None:
    """Junta el saldo de varios pozos activos en uno (los demás quedan merged)."""
    with Ledger(_db(ctx, db)) as ledger:
        try:
            pot = ledger.merge(into, sources or [])
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        console.print(f"Merge en pozo {into}: saldo {clp(pot.balance)}.")
        _print_state(console, ledger.state())


@ledger_app.command()
def plan(ctx: typer.Context, db: DbOption = None) -> None:
    """Cuota necesaria para llegar a 500.000 con los saldos actuales."""
    with Ledger(_db(ctx, db)) as ledger:
        result = ledger.plan()
        console = _console()
        console.print(result.message)
        if result.combined is None:
            return
        c = result.combined
        console.print(
            f"Juntando los pozos activos en el pozo {c.pot_id}: {clp(c.stake)} necesita cuota "
            f"{c.odds:.2f} (exacta {c.exact_odds:.4f}) para llegar a {clp(result.goal)}."
        )
        table = Table(title="Por pozo, sin juntar")
        for col in ("pozo", "saldo", "cuota exacta", "cuota mínima"):
            table.add_column(col, justify="right")
        for entry in result.per_pot:
            table.add_row(
                str(entry.pot_id), clp(entry.stake), f"{entry.exact_odds:.4f}", f"{entry.odds:.2f}"
            )
        console.print(table)
        console.print(
            "Una cuota alta implica baja probabilidad: esto es aritmética, no una recomendación."
        )


@ledger_app.command()
def status(ctx: typer.Context, db: DbOption = None) -> None:
    """Estado global, pozos y apuestas pendientes."""
    with Ledger(_db(ctx, db)) as ledger:
        console = _console()
        state = ledger.state()
        _print_state(console, state)
        pending = ledger.bets("pending")
        if pending:
            table = Table(title="Pendientes")
            for col in ("#", "pozo", "stake", "cuota", "evento", "selección", "casa"):
                table.add_column(col)
            for b in pending:
                table.add_row(
                    str(b.id),
                    ",".join(map(str, b.pot_ids)),
                    clp(b.stake),
                    f"{b.odds:.2f}",
                    b.event,
                    b.selection,
                    b.bookmaker,
                )
            console.print(table)
        if state.status == IN_PROGRESS:
            console.print("Siguiente paso: `plan` para ver la cuota necesaria.")


@ledger_app.command(name="log")
def log_cmd(ctx: typer.Context, db: DbOption = None) -> None:
    """Registro de auditoría (inmutable)."""
    with Ledger(_db(ctx, db)) as ledger:
        console = _console()
        table = Table(title="events_log")
        for col in ("#", "fecha (UTC)", "acción", "detalle"):
            table.add_column(col)
        for entry in ledger.log():
            detail = ", ".join(f"{k}={v}" for k, v in entry.payload.items())
            table.add_row(str(entry.id), entry.created_at[:19], entry.action, detail)
        console.print(table)


LEDGER_COMMANDS: dict[str, Callable[..., None]] = {
    "init": init,
    "bet": bet,
    "settle": settle,
    "merge": merge,
    "plan": plan,
    "status": status,
    "log": log_cmd,
}
