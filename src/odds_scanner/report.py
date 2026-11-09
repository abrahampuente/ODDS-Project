"""Resumen de resultados del ledger (cálculo puro + formato de texto)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from odds_scanner.ledger import GOAL, Bet, LedgerState, Pot


@dataclass(frozen=True)
class ClvRow:
    """CLV de una apuesta: cuota tomada vs mejor cuota al cierre."""

    bet_id: int
    taken_odds: float
    closing_odds: float

    @property
    def clv(self) -> float:
        """Positivo = se tomó mejor cuota que la de cierre."""
        return self.taken_odds / self.closing_odds - 1


@dataclass(frozen=True)
class Report:
    """Resumen del estado de los pozos y del desempeño de las apuestas."""

    status: str
    pots: list[Pot]
    total_balance: int
    pending_stake: int
    goal: int
    distance: int
    progress: float
    won: int
    lost: int
    void: int
    pending: int
    staked_settled: int
    returned_settled: int
    profit: int
    roi: float | None
    avg_book_margin: float | None
    avg_ev: float | None
    clv_rows: list[ClvRow]
    avg_clv: float | None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_report(
    state: LedgerState, bets: Sequence[Bet], closing_odds: Mapping[int, float]
) -> Report:
    """Calcula el reporte. `closing_odds` mapea id de apuesta -> mejor cuota al cierre."""
    by_status = {s: [b for b in bets if b.status == s] for s in ("won", "lost", "void", "pending")}
    decided = by_status["won"] + by_status["lost"]  # los void no cuentan para el ROI
    staked = sum(b.stake for b in decided)
    returned = sum(b.payout or 0 for b in decided)
    clv_rows = [ClvRow(b.id, b.odds, closing_odds[b.id]) for b in bets if b.id in closing_odds]
    return Report(
        status=state.status,
        pots=state.pots,
        total_balance=state.total_balance,
        pending_stake=state.pending_stake,
        goal=GOAL,
        distance=max(0, GOAL - state.total_balance),
        progress=state.total_balance / GOAL,
        won=len(by_status["won"]),
        lost=len(by_status["lost"]),
        void=len(by_status["void"]),
        pending=len(by_status["pending"]),
        staked_settled=staked,
        returned_settled=returned,
        profit=returned - staked,
        roi=(returned - staked) / staked if staked else None,
        avg_book_margin=_mean([b.book_margin for b in bets if b.book_margin is not None]),
        avg_ev=_mean([b.ev for b in bets if b.ev is not None]),
        clv_rows=clv_rows,
        avg_clv=_mean([c.clv for c in clv_rows]),
    )


def clp(amount: int) -> str:
    """112500 -> '112.500'."""
    return f"{amount:,}".replace(",", ".")


def _signed_clp(amount: int) -> str:
    return ("+" if amount >= 0 else "-") + clp(abs(amount))


def format_report(report: Report) -> str:
    """Reporte en texto plano."""
    lines = [f"Estado: {report.status}", "", "Saldos:"]
    lines += [f"  Pozo {p.id}: {clp(p.balance)} ({p.status})" for p in report.pots]
    lines += [
        f"  Total: {clp(report.total_balance)} | en juego: {clp(report.pending_stake)}",
        f"  Faltan para {clp(report.goal)}: {clp(report.distance)} (avance {report.progress:.1%})",
        "",
        "Apuestas:",
        f"  Ganadas: {report.won} | Perdidas: {report.lost} | Nulas: {report.void} "
        f"| Pendientes: {report.pending}",
    ]
    if report.roi is None:
        lines.append("  ROI: sin apuestas liquidadas")
    else:
        lines.append(
            f"  ROI: {report.roi:+.2%} (apostado {clp(report.staked_settled)}, "
            f"retornado {clp(report.returned_settled)}, neto {_signed_clp(report.profit)})"
        )
    lines.append(
        "  Margen promedio pagado: "
        + ("sin datos" if report.avg_book_margin is None else f"{report.avg_book_margin:.2%}")
    )
    lines.append(
        "  EV estimado promedio: "
        + ("sin datos" if report.avg_ev is None else f"{report.avg_ev:+.2%}")
    )
    if report.avg_clv is None:
        lines.append("  CLV: sin capturas de cierre para las apuestas registradas")
    else:
        lines.append(f"  CLV promedio: {report.avg_clv:+.2%} ({len(report.clv_rows)} apuestas)")
        lines += [
            f"    #{c.bet_id}: tomada {c.taken_odds:.2f} vs cierre {c.closing_odds:.2f} "
            f"({c.clv:+.2%})"
            for c in report.clv_rows
        ]
    return "\n".join(lines)
