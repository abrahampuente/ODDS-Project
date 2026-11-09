"""Ledger de 4 pozos de 25.000 CLP con meta de 500.000 CLP, en SQLite.

Solo REGISTRA apuestas hechas por el usuario: nunca coloca apuestas reales.
Dinero siempre en enteros (CLP). Cada operación queda en events_log, que es
inmutable (triggers impiden UPDATE/DELETE).
"""

import json
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn, Self

from odds_scanner.math import target_odds

N_POTS = 4
POT_INITIAL = 25_000
GOAL = 500_000
MIN_ODDS = 1.01  # cuota mínima que ofrece una casa; por debajo no hay ganancia posible
DEFAULT_DB = Path("data/ledger.db")

NOT_INITIALIZED = "SIN INICIAR"
IN_PROGRESS = "EN CURSO"
GOAL_REACHED = "META CUMPLIDA"
BUDGET_EXHAUSTED = "PRESUPUESTO AGOTADO"

PotStatus = Literal["active", "busted", "merged", "done"]
BetStatus = Literal["pending", "won", "lost", "void"]
SETTLE_RESULTS: tuple[BetStatus, ...] = ("won", "lost", "void")

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        f"""
        CREATE TABLE pots (
            id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND {N_POTS}),
            initial INTEGER NOT NULL CHECK (initial = {POT_INITIAL}),
            balance INTEGER NOT NULL CHECK (balance >= 0),
            status TEXT NOT NULL CHECK (status IN ('active', 'busted', 'merged', 'done'))
        );
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pot_ids TEXT NOT NULL,
            stake INTEGER NOT NULL CHECK (stake > 0),
            odds REAL NOT NULL CHECK (odds > 1.0),
            event TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            bookmaker TEXT NOT NULL,
            placed_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'won', 'lost', 'void')),
            payout INTEGER CHECK (payout IS NULL OR payout >= 0),
            settled_at TEXT,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE events_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TRIGGER events_log_no_update BEFORE UPDATE ON events_log
        BEGIN SELECT RAISE(ABORT, 'events_log es inmutable'); END;
        CREATE TRIGGER events_log_no_delete BEFORE DELETE ON events_log
        BEGIN SELECT RAISE(ABORT, 'events_log es inmutable'); END;
        CREATE INDEX bets_status ON bets (status);
        """,
    ),
    (
        3,
        """
        ALTER TABLE bets ADD COLUMN event_id TEXT;
        ALTER TABLE bets ADD COLUMN commence_time TEXT;
        ALTER TABLE bets ADD COLUMN line REAL;
        ALTER TABLE bets ADD COLUMN book_margin REAL;
        ALTER TABLE bets ADD COLUMN fair_prob REAL;
        ALTER TABLE bets ADD COLUMN ev REAL;
        ALTER TABLE bets ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
            CHECK (source IN ('manual', 'row'));
        """,
    ),
]
SCHEMA_VERSION = 3


class LedgerError(RuntimeError):
    """Error de uso del ledger (p. ej. id inexistente)."""


class RuleViolationError(LedgerError):
    """La operación viola una regla de negocio."""


@dataclass(frozen=True)
class Pot:
    """Un pozo."""

    id: int
    initial: int
    balance: int
    status: PotStatus


@dataclass(frozen=True)
class Bet:
    """Una apuesta registrada."""

    id: int
    pot_ids: list[int]
    stake: int
    odds: float
    event: str
    market: str
    selection: str
    bookmaker: str
    placed_at: str
    status: BetStatus
    payout: int | None
    settled_at: str | None
    notes: str
    event_id: str | None = None
    commence_time: str | None = None
    line: float | None = None
    book_margin: float | None = None
    fair_prob: float | None = None
    ev: float | None = None
    source: str = "manual"


@dataclass(frozen=True)
class LogEntry:
    """Entrada de auditoría."""

    id: int
    created_at: str
    action: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class LedgerState:
    """Estado global de los pozos."""

    status: str
    total_balance: int
    pending_stake: int
    pots: list[Pot]


@dataclass(frozen=True)
class PlanEntry:
    """Cuota necesaria para llevar `stake` a la meta en una sola apuesta."""

    pot_id: int
    stake: int
    exact_odds: float
    odds: float  # redondeada hacia arriba a 2 decimales: alcanza la meta


@dataclass(frozen=True)
class Plan:
    """Plan para llegar a la meta con los saldos actuales."""

    goal: int
    combined: PlanEntry | None  # juntando todos los pozos activos en el primero
    per_pot: list[PlanEntry]
    message: str


def payout_for(stake: int, odds: float) -> int:
    """Pago bruto de una apuesta ganada, redondeado al peso (mitad hacia arriba)."""
    exact = Decimal(stake) * Decimal(str(odds))
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _plan_entry(pot_id: int, stake: int) -> PlanEntry:
    exact = target_odds(stake, GOAL)
    odds = math.ceil(exact * 100 - 1e-9) / 100
    while payout_for(stake, odds) < GOAL:  # por si el redondeo deja corto
        odds = round(odds + 0.01, 2)
    return PlanEntry(pot_id=pot_id, stake=stake, exact_odds=exact, odds=odds)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Ledger:
    """Acceso al ledger. Cada operación es atómica (todo o nada)."""

    def __init__(self, path: Path = DEFAULT_DB, *, clock: Callable[[], datetime] = _utcnow):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        try:
            self._migrate()
        except BaseException:
            self._conn.close()  # sin esto la conexión queda abierta si la migración falla
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Cierra la conexión."""
        self._conn.close()

    # --- migraciones ---

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current = self.schema_version()
        for version, sql in MIGRATIONS:  # global: se lee en cada apertura
            if version <= current:
                continue
            # El DDL y el registro de versión van en una sola transacción: o se aplica
            # todo o nada. La versión se inserta con parámetros (sin armar SQL a mano).
            try:
                self._conn.executescript("BEGIN;\n" + sql)
                self._conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, self._now()),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def schema_version(self) -> int:
        """Versión del esquema aplicada."""
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    # --- lectura ---

    def _now(self) -> str:
        return self._clock().isoformat()

    def pots(self) -> list[Pot]:
        """Todos los pozos ordenados por id."""
        rows = self._conn.execute("SELECT * FROM pots ORDER BY id").fetchall()
        return [Pot(r["id"], r["initial"], r["balance"], r["status"]) for r in rows]

    def pot(self, pot_id: int) -> Pot:
        """Un pozo; LedgerError si no existe."""
        for pot in self.pots():
            if pot.id == pot_id:
                return pot
        raise LedgerError(f"el pozo {pot_id} no existe")

    def bets(self, status: BetStatus | None = None) -> list[Bet]:
        """Apuestas, opcionalmente filtradas por estado."""
        query = "SELECT * FROM bets"
        params: tuple[str, ...] = ()
        if status is not None:
            query, params = query + " WHERE status = ?", (status,)
        rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [self._bet(r) for r in rows]

    def bet(self, bet_id: int) -> Bet:
        """Una apuesta; LedgerError si no existe."""
        row = self._conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
        if row is None:
            raise LedgerError(f"la apuesta {bet_id} no existe")
        return self._bet(row)

    @staticmethod
    def _bet(r: sqlite3.Row) -> Bet:
        return Bet(
            id=r["id"],
            pot_ids=json.loads(r["pot_ids"]),
            stake=r["stake"],
            odds=r["odds"],
            event=r["event"],
            market=r["market"],
            selection=r["selection"],
            bookmaker=r["bookmaker"],
            placed_at=r["placed_at"],
            status=r["status"],
            payout=r["payout"],
            settled_at=r["settled_at"],
            notes=r["notes"],
            event_id=r["event_id"],
            commence_time=r["commence_time"],
            line=r["line"],
            book_margin=r["book_margin"],
            fair_prob=r["fair_prob"],
            ev=r["ev"],
            source=r["source"],
        )

    def log(self) -> list[LogEntry]:
        """Registro de auditoría completo."""
        rows = self._conn.execute("SELECT * FROM events_log ORDER BY id").fetchall()
        return [
            LogEntry(r["id"], r["created_at"], r["action"], json.loads(r["payload"])) for r in rows
        ]

    def _pending_stake(self, pot_id: int | None = None) -> int:
        query = "SELECT COALESCE(SUM(stake), 0) AS s FROM bets WHERE status = 'pending'"
        params: tuple[str, ...] = ()
        if pot_id is not None:
            query += " AND pot_ids = ?"
            params = (json.dumps([pot_id]),)
        return int(self._conn.execute(query, params).fetchone()["s"])

    def state(self) -> LedgerState:
        """Estado global: SIN INICIAR, EN CURSO, META CUMPLIDA o PRESUPUESTO AGOTADO."""
        pots = self.pots()
        total = sum(p.balance for p in pots)
        pending = self._pending_stake()
        if not pots:
            status = NOT_INITIALIZED
        elif total >= GOAL:
            status = GOAL_REACHED
        elif total == 0 and pending == 0:
            status = BUDGET_EXHAUSTED
        else:
            status = IN_PROGRESS
        return LedgerState(status, total, pending, pots)

    # --- escritura ---

    def _log(self, action: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events_log (created_at, action, payload) VALUES (?, ?, ?)",
            (self._now(), action, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def init(self, *, reset: bool = False, confirm: bool = False) -> list[Pot]:
        """Crea los 4 pozos. Solo una vez; reiniciar exige reset=True y confirm=True.

        El reinicio solo se permite si no hay apuestas registradas: con apuestas
        (pendientes o liquidadas) volver a 4 x 25.000 sería una recarga encubierta.
        """
        if self.pots():
            if not reset:
                raise RuleViolationError(
                    "los pozos ya existen; para reiniciar usa --reset --confirm"
                )
            if not confirm:
                raise RuleViolationError("el reinicio recrea los 4 pozos: agrega --confirm")
        with self._conn:
            if reset:
                summary = {"pots": [p.__dict__ for p in self.pots()]}
                # El DELETE toma el bloqueo de escritura antes de contar apuestas: ningún
                # otro proceso puede registrar una entre la verificación y el reinicio.
                self._conn.execute("DELETE FROM pots")
                n_bets = int(self._conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0])
                if n_bets:
                    raise RuleViolationError(
                        f"no se puede reiniciar: hay {n_bets} apuesta(s) registrada(s) y "
                        "volver a 4 x 25.000 sería una recarga (prohibida)"
                    )
                self._log("reset", summary)
            self._conn.executemany(
                "INSERT INTO pots (id, initial, balance, status) VALUES (?, ?, ?, 'active')",
                [(i, POT_INITIAL, POT_INITIAL) for i in range(1, N_POTS + 1)],
            )
            self._log("init", {"pots": N_POTS, "initial": POT_INITIAL})
        return self.pots()

    def deposit(self, pot_id: int, amount: int) -> NoReturn:
        """Siempre falla: está prohibido recargar pozos. El intento queda auditado."""
        with self._conn:
            self._log("deposit_rejected", {"pot_id": pot_id, "amount": amount})
        raise RuleViolationError("prohibido recargar pozos o depositar más dinero")

    def _require_betting_allowed(self) -> None:
        status = self.state().status
        if status == NOT_INITIALIZED:
            raise RuleViolationError("no hay pozos: ejecuta `ledger init` primero")
        if status in (GOAL_REACHED, BUDGET_EXHAUSTED):
            raise RuleViolationError(f"{status}: no se aceptan nuevas apuestas")

    def check_bet(self, pot_id: int, stake: int, odds: float) -> Pot:
        """Valida una apuesta sin registrarla; lanza RuleViolationError si no procede."""
        self._require_betting_allowed()
        pot = self.pot(pot_id)
        if pot.status != "active":
            raise RuleViolationError(f"el pozo {pot_id} está {pot.status}")
        if stake <= 0:
            raise RuleViolationError("el stake debe ser un entero positivo (CLP)")
        if not math.isfinite(odds) or odds < MIN_ODDS:
            raise RuleViolationError(f"cuota inválida: {odds} (debe ser >= {MIN_ODDS:.2f})")
        if stake > pot.balance:
            raise RuleViolationError(
                f"stake {stake} supera el saldo del pozo {pot_id} ({pot.balance})"
            )
        return pot

    def place_bet(
        self,
        *,
        pot_id: int,
        stake: int,
        odds: float,
        event: str,
        market: str,
        selection: str,
        bookmaker: str,
        notes: str = "",
        event_id: str | None = None,
        commence_time: str | None = None,
        line: float | None = None,
        book_margin: float | None = None,
        fair_prob: float | None = None,
        ev: float | None = None,
        source: Literal["manual", "row"] = "manual",
    ) -> Bet:
        """Registra una apuesta y descuenta el stake del saldo del pozo."""
        self.check_bet(pot_id, stake, odds)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO bets (pot_ids, stake, odds, event, market, selection, bookmaker,"
                " placed_at, status, notes, event_id, commence_time, line, book_margin,"
                " fair_prob, ev, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    json.dumps([pot_id]),
                    stake,
                    odds,
                    event,
                    market,
                    selection,
                    bookmaker,
                    self._now(),
                    notes,
                    event_id,
                    commence_time,
                    line,
                    book_margin,
                    fair_prob,
                    ev,
                    source,
                ),
            )
            # Descuento condicional: si otro proceso gastó el saldo entre check_bet y
            # esta escritura, no se toca nada y la transacción completa se revierte.
            debited = self._conn.execute(
                "UPDATE pots SET balance = balance - ?"
                " WHERE id = ? AND status = 'active' AND balance >= ?",
                (stake, pot_id, stake),
            ).rowcount
            if debited != 1:
                raise RuleViolationError(
                    f"stake {stake} supera el saldo actual del pozo {pot_id} "
                    "(cambió durante el registro)"
                )
            bet_id = int(cur.lastrowid or 0)
            self._log(
                "bet",
                {
                    "bet_id": bet_id,
                    "pot_id": pot_id,
                    "stake": stake,
                    "odds": odds,
                    "event": event,
                    "market": market,
                    "selection": selection,
                    "bookmaker": bookmaker,
                    "source": source,
                },
            )
        return self.bet(bet_id)

    def settle(self, bet_id: int, result: str) -> Bet:
        """Liquida: won suma stake*odds, lost no devuelve nada, void devuelve el stake."""
        if result not in SETTLE_RESULTS:
            raise RuleViolationError(f"resultado inválido: {result!r} (won, lost o void)")
        current = self.bet(bet_id)
        if current.status != "pending":
            raise RuleViolationError(f"la apuesta {bet_id} ya fue liquidada ({current.status})")
        payout = {
            "won": payout_for(current.stake, current.odds),
            "lost": 0,
            "void": current.stake,
        }[result]
        [pot_id] = current.pot_ids
        with self._conn:
            # Compare-and-set: solo liquida si sigue pendiente; evita el doble pago
            # cuando dos procesos liquidan la misma apuesta a la vez.
            updated = self._conn.execute(
                "UPDATE bets SET status = ?, payout = ?, settled_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (result, payout, self._now(), bet_id),
            ).rowcount
            if updated != 1:
                raise RuleViolationError(
                    f"la apuesta {bet_id} ya fue liquidada ({self.bet(bet_id).status})"
                )
            self._conn.execute(
                "UPDATE pots SET balance = balance + ? WHERE id = ?", (payout, pot_id)
            )
            self._log(
                "settle",
                {"bet_id": bet_id, "pot_id": pot_id, "result": result, "payout": payout},
            )
            self._refresh_statuses()
        return self.bet(bet_id)

    def _refresh_statuses(self) -> None:
        """Marca busted los pozos en 0 sin pendientes y done los activos si se llegó a la meta."""
        for pot in self.pots():
            if pot.status == "active" and pot.balance == 0 and self._pending_stake(pot.id) == 0:
                self._conn.execute("UPDATE pots SET status = 'busted' WHERE id = ?", (pot.id,))
        if self.state().total_balance >= GOAL:
            self._conn.execute("UPDATE pots SET status = 'done' WHERE status = 'active'")

    def merge(self, into: int, sources: Sequence[int]) -> Pot:
        """Junta el saldo de `sources` en `into`; los de origen quedan merged."""
        self._require_betting_allowed()
        if not sources:
            raise RuleViolationError("indica al menos un pozo de origen")
        if len(set(sources)) != len(sources) or into in sources:
            raise RuleViolationError("pozos repetidos en el merge")
        involved = [self.pot(into), *(self.pot(s) for s in sources)]
        for pot in involved:
            if pot.status != "active":
                raise RuleViolationError(f"el pozo {pot.id} está {pot.status}")
            if self._pending_stake(pot.id):
                raise RuleViolationError(f"el pozo {pot.id} tiene apuestas pendientes")
        # Las validaciones de arriba dan mensajes claros; las de abajo se repiten dentro de
        # la transacción (UPDATE condicional) por si otro proceso apostó en el intertanto.
        mergeable = (
            "id = ? AND status = 'active' AND NOT EXISTS "
            "(SELECT 1 FROM bets WHERE status = 'pending' AND pot_ids = ?)"
        )

        def changed_meanwhile(pot_id: int) -> RuleViolationError:
            return RuleViolationError(
                f"el pozo {pot_id} cambió durante el merge (apuesta nueva o estado distinto)"
            )

        moved = 0
        with self._conn:
            locked = self._conn.execute(
                f"UPDATE pots SET balance = balance WHERE {mergeable}",  # nosec B608
                (into, json.dumps([into])),
            ).rowcount
            if locked != 1:
                raise changed_meanwhile(into)
            for source in sources:
                moved += self.pot(source).balance  # leído con el bloqueo de escritura tomado
                updated = self._conn.execute(
                    f"UPDATE pots SET balance = 0, status = 'merged' WHERE {mergeable}",  # nosec B608
                    (source, json.dumps([source])),
                ).rowcount
                if updated != 1:
                    raise changed_meanwhile(source)
            self._conn.execute("UPDATE pots SET balance = balance + ? WHERE id = ?", (moved, into))
            self._log("merge", {"into": into, "sources": list(sources), "moved": moved})
        return self.pot(into)

    # --- plan ---

    def plan(self) -> Plan:
        """Cuota necesaria para llegar a la meta: juntando pozos activos y por pozo."""
        state = self.state()
        if state.status == NOT_INITIALIZED:
            return Plan(GOAL, None, [], "No hay pozos: ejecuta `ledger init`.")
        if state.status == GOAL_REACHED:
            return Plan(GOAL, None, [], f"{GOAL_REACHED}: no hay nada más que planificar.")
        if state.status == BUDGET_EXHAUSTED:
            return Plan(
                GOAL,
                None,
                [],
                f"{BUDGET_EXHAUSTED}: no hay saldo y no se permiten recargas. Fin del plan.",
            )
        usable = [p for p in state.pots if p.status == "active" and p.balance > 0]
        if not usable:
            return Plan(GOAL, None, [], "No hay saldo disponible: espera las apuestas pendientes.")
        per_pot = [_plan_entry(p.id, p.balance) for p in usable]
        combined = _plan_entry(usable[0].id, sum(p.balance for p in usable))
        return Plan(GOAL, combined, per_pot, "Cuotas necesarias para llegar a la meta.")
