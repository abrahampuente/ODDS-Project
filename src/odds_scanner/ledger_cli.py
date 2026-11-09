"""Comandos `odds-scanner ledger ...` (I/O de consola sobre ledger.py)."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from odds_scanner.ledger import (
    DEFAULT_DB,
    GOAL,
    IN_PROGRESS,
    Ledger,
    LedgerError,
    LedgerState,
    payout_for,
)

ledger_app = typer.Typer(
    no_args_is_help=True,
    help="Registro de los 4 pozos (solo registra; nunca coloca apuestas reales).",
)


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


def _open(ctx: typer.Context) -> Ledger:
    db: Path = ctx.obj["db"]
    return Ledger(db)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"ERROR: {message}")
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
) -> None:
    """Crea los 4 pozos de 25.000 (solo una vez)."""
    with _open(ctx) as ledger:
        try:
            ledger.init(reset=reset, confirm=confirm)
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        console.print(f"Pozos creados en {ledger.path}" + (" (reinicio)" if reset else ""))
        _print_state(console, ledger.state())


@ledger_app.command()
def bet(
    ctx: typer.Context,
    pot: Annotated[int, typer.Option(help="Pozo (1-4).")],
    stake: Annotated[int, typer.Option(help="Monto en CLP (entero).")],
    odds: Annotated[float, typer.Option(help="Cuota decimal.")],
    event: Annotated[str, typer.Option(help="Evento.")],
    market: Annotated[str, typer.Option(help="Mercado (h2h, totals...).")],
    selection: Annotated[str, typer.Option(help="Resultado elegido.")],
    bookmaker: Annotated[str, typer.Option(help="Casa de apuestas.")],
    notes: Annotated[str, typer.Option(help="Notas.")] = "",
) -> None:
    """Registra una apuesta hecha por ti y descuenta el stake del pozo."""
    with _open(ctx) as ledger:
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
            )
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        console.print(
            f"Apuesta #{placed.id} registrada: pozo {pot}, {clp(stake)} a {odds} "
            f"({selection} en {event}, {bookmaker}). Si gana paga {clp(payout_for(stake, odds))}."
        )
        console.print(f"Saldo del pozo {pot}: {clp(ledger.pot(pot).balance)}")
        console.print("Recordatorio: odds-scanner solo registra; no coloca apuestas.")


@ledger_app.command()
def settle(
    ctx: typer.Context,
    bet_id: Annotated[int, typer.Argument(help="Id de la apuesta.")],
    result: Annotated[SettleResult, typer.Argument(help="won, lost o void.")],
) -> None:
    """Liquida una apuesta pendiente."""
    with _open(ctx) as ledger:
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
) -> None:
    """Junta el saldo de varios pozos activos en uno."""
    with _open(ctx) as ledger:
        try:
            pot = ledger.merge(into, sources or [])
        except LedgerError as exc:
            raise _fail(str(exc)) from None
        console = _console()
        console.print(f"Merge en pozo {into}: saldo {clp(pot.balance)}.")
        _print_state(console, ledger.state())


@ledger_app.command()
def plan(ctx: typer.Context) -> None:
    """Cuota necesaria para llegar a 500.000 con los saldos actuales."""
    with _open(ctx) as ledger:
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
def status(ctx: typer.Context) -> None:
    """Estado global, pozos y apuestas pendientes."""
    with _open(ctx) as ledger:
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
            console.print("Siguiente paso: `ledger plan` para ver la cuota necesaria.")


@ledger_app.command(name="log")
def log_cmd(ctx: typer.Context) -> None:
    """Registro de auditoría (inmutable)."""
    with _open(ctx) as ledger:
        console = _console()
        table = Table(title="events_log")
        for col in ("#", "fecha (UTC)", "acción", "detalle"):
            table.add_column(col)
        for entry in ledger.log():
            detail = ", ".join(f"{k}={v}" for k, v in entry.payload.items())
            table.add_row(str(entry.id), entry.created_at[:19], entry.action, detail)
        console.print(table)
