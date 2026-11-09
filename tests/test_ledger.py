"""Tests del ledger de pozos: 8 reglas de negocio, auditoría y simulaciones."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from odds_scanner.ledger import (
    BUDGET_EXHAUSTED,
    GOAL,
    GOAL_REACHED,
    IN_PROGRESS,
    NOT_INITIALIZED,
    POT_INITIAL,
    SCHEMA_VERSION,
    Ledger,
    LedgerError,
    RuleViolationError,
    payout_for,
)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.db", clock=lambda: datetime(2026, 11, 9, 12, tzinfo=UTC)) as lg:
        yield lg


@pytest.fixture
def ready(ledger: Ledger) -> Ledger:
    ledger.init()
    return ledger


def bet(lg: Ledger, pot: int, stake: int, odds: float, selection: str = "Local") -> int:
    return lg.place_bet(
        pot_id=pot,
        stake=stake,
        odds=odds,
        event="Local vs Visita",
        market="h2h",
        selection=selection,
        bookmaker="casa",
    ).id


# --- esquema y migraciones ----------------------------------------------------


def test_migrations_create_schema_version(ledger: Ledger, tmp_path: Path) -> None:
    assert ledger.schema_version() == SCHEMA_VERSION
    ledger.close()
    with Ledger(tmp_path / "ledger.db") as again:  # reabrir no re-aplica migraciones
        assert again.schema_version() == SCHEMA_VERSION
    conn = sqlite3.connect(tmp_path / "ledger.db")
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    conn.close()
    assert [r[0] for r in rows] == list(range(1, SCHEMA_VERSION + 1))


def test_creates_parent_directory(tmp_path: Path) -> None:
    with Ledger(tmp_path / "nested" / "dir" / "ledger.db") as lg:
        assert lg.state().status == NOT_INITIALIZED


# --- regla 1: init ------------------------------------------------------------


def test_rule1_init_creates_four_pots_of_25000(ledger: Ledger) -> None:
    pots = ledger.init()
    assert [p.id for p in pots] == [1, 2, 3, 4]
    assert all(p.initial == POT_INITIAL == 25_000 for p in pots)
    assert all(p.balance == 25_000 and p.status == "active" for p in pots)
    state = ledger.state()
    assert state.total_balance == 100_000
    assert state.status == IN_PROGRESS


def test_rule1_init_twice_fails(ready: Ledger) -> None:
    with pytest.raises(RuleViolationError, match="--reset --confirm"):
        ready.init()


def test_rule1_reset_requires_confirm(ready: Ledger) -> None:
    with pytest.raises(RuleViolationError, match="--confirm"):
        ready.init(reset=True)


def test_rule1_reset_with_confirm_restarts_but_keeps_audit(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_000, 2.0)
    ready.settle(bet_id, "lost")
    pots = ready.init(reset=True, confirm=True)
    assert sum(p.balance for p in pots) == 100_000
    assert ready.bets() == []
    actions = [e.action for e in ready.log()]
    assert actions.count("init") == 2
    assert "reset" in actions and "bet" in actions  # la auditoría no se borra


# --- regla 2: sin recargas ----------------------------------------------------


def test_rule2_deposit_is_forbidden_and_audited(ready: Ledger) -> None:
    with pytest.raises(RuleViolationError, match="recargar"):
        ready.deposit(1, 5_000)
    assert ready.pot(1).balance == 25_000
    assert ready.log()[-1].action == "deposit_rejected"


def test_rule2_balance_cannot_exceed_initial_without_winnings(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_000, 3.0)
    ready.settle(bet_id, "void")
    assert ready.pot(1).balance == 25_000  # el void devuelve, no agrega


# --- regla 3: stake <= saldo, saldo nunca negativo ----------------------------


def test_rule3_stake_cannot_exceed_balance(ready: Ledger) -> None:
    with pytest.raises(RuleViolationError, match="saldo"):
        bet(ready, 1, 25_001, 2.0)
    assert ready.pot(1).balance == 25_000


def test_rule3_pending_bets_reduce_available_balance(ready: Ledger) -> None:
    bet(ready, 1, 20_000, 2.0)
    assert ready.pot(1).balance == 5_000
    with pytest.raises(RuleViolationError):
        bet(ready, 1, 5_001, 2.0)
    bet(ready, 1, 5_000, 2.0)
    assert ready.pot(1).balance == 0


@pytest.mark.parametrize(("stake", "odds"), [(0, 2.0), (-100, 2.0), (1_000, 1.0), (1_000, 0.5)])
def test_rule3_rejects_invalid_stake_or_odds(ready: Ledger, stake: int, odds: float) -> None:
    with pytest.raises(RuleViolationError):
        bet(ready, 1, stake, odds)


def test_rule3_database_rejects_negative_balance(ready: Ledger, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pots SET balance = -1 WHERE id = 1")
    conn.close()


def test_bet_on_unknown_or_inactive_pot_fails(ready: Ledger) -> None:
    with pytest.raises(LedgerError):
        bet(ready, 9, 1_000, 2.0)
    ready.merge(1, [2])
    with pytest.raises(RuleViolationError, match="merged"):
        bet(ready, 2, 1, 2.0)


def test_bet_before_init_fails(ledger: Ledger) -> None:
    with pytest.raises(RuleViolationError, match="init"):
        bet(ledger, 1, 1_000, 2.0)


# --- regla 4: settle ----------------------------------------------------------


def test_rule4_won_adds_stake_times_odds_rounded(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_001, 1.85)  # 18501.85 -> 18502
    settled = ready.settle(bet_id, "won")
    assert settled.payout == 18_502
    assert settled.status == "won" and settled.settled_at is not None
    assert ready.pot(1).balance == 25_000 - 10_001 + 18_502


@pytest.mark.parametrize(
    ("stake", "odds", "payout"),
    [(25_000, 4.5, 112_500), (1_001, 2.5, 2_503), (1_000, 1.0005, 1_001), (333, 3.0, 999)],
)
def test_payout_rounds_half_up_to_peso(stake: int, odds: float, payout: int) -> None:
    assert payout_for(stake, odds) == payout  # 2502.5 -> 2503, 1000.5 -> 1001


def test_rule4_lost_keeps_stake_deducted(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_000, 2.0)
    settled = ready.settle(bet_id, "lost")
    assert settled.payout == 0
    assert ready.pot(1).balance == 15_000


def test_rule4_void_returns_stake(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_000, 2.0)
    assert ready.settle(bet_id, "void").payout == 10_000
    assert ready.pot(1).balance == 25_000


def test_rule4_settle_only_once_and_valid_result(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 10_000, 2.0)
    with pytest.raises(RuleViolationError):
        ready.settle(bet_id, "pending")
    ready.settle(bet_id, "won")
    with pytest.raises(RuleViolationError, match="liquidada"):
        ready.settle(bet_id, "lost")
    with pytest.raises(LedgerError):
        ready.settle(999, "won")


def test_rule4_pot_busted_when_zero_and_no_pending(ready: Ledger) -> None:
    first = bet(ready, 1, 20_000, 2.0)
    second = bet(ready, 1, 5_000, 2.0)
    ready.settle(first, "lost")
    assert ready.pot(1).status == "active"  # aún hay una pendiente
    ready.settle(second, "lost")
    assert ready.pot(1).status == "busted"


# --- regla 5: merge -----------------------------------------------------------


def test_rule5_merge_moves_balances_and_marks_sources(ready: Ledger) -> None:
    pot = ready.merge(1, [2, 3])
    assert pot.balance == 75_000
    assert [(p.id, p.balance, p.status) for p in ready.pots()] == [
        (1, 75_000, "active"),
        (2, 0, "merged"),
        (3, 0, "merged"),
        (4, 25_000, "active"),
    ]
    assert ready.state().total_balance == 100_000  # el merge no crea dinero


@pytest.mark.parametrize("sources", [[], [1], [9], [2, 2]])
def test_rule5_merge_rejects_invalid_sources(ready: Ledger, sources: list[int]) -> None:
    with pytest.raises(LedgerError):
        ready.merge(1, sources)


def test_rule5_merge_requires_active_pots_without_pending(ready: Ledger) -> None:
    ready.merge(1, [2])
    with pytest.raises(RuleViolationError, match="merged"):
        ready.merge(1, [2])
    bet(ready, 3, 1_000, 2.0)
    with pytest.raises(RuleViolationError, match="pendientes"):
        ready.merge(1, [3])


# --- regla 6: meta ------------------------------------------------------------


def test_rule6_goal_reached_blocks_new_bets(ready: Ledger) -> None:
    ready.merge(1, [2, 3, 4])
    bet_id = bet(ready, 1, 100_000, 5.0)
    ready.settle(bet_id, "won")
    state = ready.state()
    assert state.total_balance == 500_000 >= GOAL
    assert state.status == GOAL_REACHED == "META CUMPLIDA"
    assert ready.pot(1).status == "done"
    with pytest.raises(RuleViolationError, match="META CUMPLIDA"):
        bet(ready, 1, 1_000, 2.0)


# --- regla 7: presupuesto agotado ---------------------------------------------


def test_rule7_all_pots_zero_is_budget_exhausted(ready: Ledger) -> None:
    for pot in range(1, 5):
        ready.settle(bet(ready, pot, 25_000, 2.0), "lost")
    state = ready.state()
    assert state.total_balance == 0
    assert state.status == BUDGET_EXHAUSTED == "PRESUPUESTO AGOTADO"
    assert all(p.status == "busted" for p in ready.pots())
    with pytest.raises(RuleViolationError, match="PRESUPUESTO AGOTADO"):
        bet(ready, 1, 1, 2.0)
    plan = ready.plan()
    assert plan.combined is None and plan.per_pot == []
    assert "no hay" in plan.message.lower()


def test_rule7_not_exhausted_while_bets_pending(ready: Ledger) -> None:
    for pot in range(1, 5):
        bet(ready, pot, 25_000, 2.0)
    assert ready.state().status == IN_PROGRESS


# --- regla 8: plan ------------------------------------------------------------


def test_rule8_plan_initial_state(ready: Ledger) -> None:
    plan = ready.plan()
    assert plan.combined is not None
    assert plan.combined.stake == 100_000
    assert plan.combined.exact_odds == pytest.approx(5.0)
    assert [(p.pot_id, p.stake, p.exact_odds) for p in plan.per_pot] == [
        (1, 25_000, 20.0),
        (2, 25_000, 20.0),
        (3, 25_000, 20.0),
        (4, 25_000, 20.0),
    ]


def test_rule8_plan_odds_rounded_up_still_reach_goal(ready: Ledger) -> None:
    ready.settle(bet(ready, 1, 25_000, 4.5), "won")  # pozo 1 = 112.500
    ready.merge(1, [2, 3, 4])  # 187.500
    plan = ready.plan()
    assert plan.combined is not None
    assert plan.combined.exact_odds == pytest.approx(500_000 / 187_500)  # 2.6667
    assert plan.combined.odds == 2.67  # redondeo hacia arriba a 2 decimales
    assert payout_for(187_500, plan.combined.odds) >= GOAL


def test_rule8_plan_ignores_zero_and_inactive_pots(ready: Ledger) -> None:
    ready.merge(1, [2])
    ready.settle(bet(ready, 3, 25_000, 2.0), "lost")
    plan = ready.plan()
    assert [p.pot_id for p in plan.per_pot] == [1, 4]
    assert plan.combined is not None and plan.combined.stake == 75_000


def test_plan_after_goal_says_reached(ready: Ledger) -> None:
    ready.merge(1, [2, 3, 4])
    ready.settle(bet(ready, 1, 100_000, 5.0), "won")
    plan = ready.plan()
    assert plan.combined is None and "META CUMPLIDA" in plan.message


def test_plan_before_init(ledger: Ledger) -> None:
    assert "init" in ledger.plan().message


# --- auditoría ----------------------------------------------------------------


def test_events_log_records_every_operation(ready: Ledger) -> None:
    bet_id = bet(ready, 1, 5_000, 2.0)
    ready.settle(bet_id, "won")
    ready.merge(1, [2])
    actions = [e.action for e in ready.log()]
    assert actions == ["init", "bet", "settle", "merge"]
    assert all(e.created_at for e in ready.log())


def test_events_log_is_immutable(ready: Ledger, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    with pytest.raises(sqlite3.DatabaseError, match="inmutable"):
        conn.execute("UPDATE events_log SET action = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="inmutable"):
        conn.execute("DELETE FROM events_log")
    conn.close()


def test_failed_operation_leaves_no_partial_changes(ready: Ledger) -> None:
    before = (ready.pots(), len(ready.log()))
    with pytest.raises(RuleViolationError):
        ready.merge(1, [2, 2])
    with pytest.raises(LedgerError):
        ready.merge(1, [2, 9])
    assert (ready.pots(), len(ready.log())) == before


# --- simulaciones completas ---------------------------------------------------


def test_simulation_win_merge_plan_win_goal(ready: Ledger) -> None:
    # 1) pozo 1 gana a 4.5
    b1 = bet(ready, 1, 25_000, 4.5)
    ready.settle(b1, "won")
    assert ready.pot(1).balance == 112_500
    # 2) merge con pozos 2-4
    merged = ready.merge(1, [2, 3, 4])
    assert merged.balance == 187_500
    # 3) apuesta todo a la cuota del plan
    plan = ready.plan()
    assert plan.combined is not None and plan.combined.pot_id == 1
    b2 = bet(ready, 1, plan.combined.stake, plan.combined.odds)
    # 4) gana -> META CUMPLIDA
    ready.settle(b2, "won")
    state = ready.state()
    assert state.total_balance == 500_625  # 187.500 x 2.67
    assert state.status == GOAL_REACHED
    # 5) la siguiente apuesta es rechazada
    with pytest.raises(RuleViolationError, match="META CUMPLIDA"):
        bet(ready, 1, 1_000, 1.5)
    assert [e.action for e in ready.log()] == ["init", "bet", "settle", "merge", "bet", "settle"]


def test_simulation_all_four_lose(ready: Ledger) -> None:
    for pot in range(1, 5):
        ready.settle(bet(ready, pot, 25_000, 3.0), "lost")
    state = ready.state()
    assert state.status == BUDGET_EXHAUSTED
    assert state.total_balance == 0 and state.pending_stake == 0
    assert [b.status for b in ready.bets()] == ["lost"] * 4
    with pytest.raises(RuleViolationError, match="PRESUPUESTO AGOTADO"):
        bet(ready, 2, 1, 2.0)
    with pytest.raises(RuleViolationError, match="recargar"):
        ready.deposit(1, 25_000)


# --- migración v3: contexto de la apuesta (fase 7) ----------------------------


def test_bet_stores_context_fields(ready: Ledger) -> None:
    placed = ready.place_bet(
        pot_id=1,
        stake=1_000,
        odds=2.5,
        event="A vs B",
        market="totals",
        selection="Over",
        bookmaker="pinnacle",
        event_id="ev1",
        commence_time="2026-11-21T15:00:00+00:00",
        line=2.5,
        book_margin=0.031,
        fair_prob=0.42,
        ev=0.05,
        source="row",
    )
    assert (placed.event_id, placed.line, placed.source) == ("ev1", 2.5, "row")
    assert (placed.book_margin, placed.fair_prob, placed.ev) == (0.031, 0.42, 0.05)
    assert placed.commence_time == "2026-11-21T15:00:00+00:00"


def test_manual_bet_defaults(ready: Ledger) -> None:
    placed = ready.bet(bet(ready, 1, 1_000, 2.0))
    assert placed.source == "manual"
    assert placed.event_id is None and placed.book_margin is None and placed.ev is None


def test_check_bet_validates_without_writing(ready: Ledger) -> None:
    ready.check_bet(1, 25_000, 2.0)
    with pytest.raises(RuleViolationError, match="saldo"):
        ready.check_bet(1, 25_001, 2.0)
    assert ready.bets() == [] and [e.action for e in ready.log()] == ["init"]


def test_v2_database_is_migrated_to_v3(tmp_path: Path) -> None:
    import odds_scanner.ledger as ledger_module

    path = tmp_path / "old.db"
    original = ledger_module.MIGRATIONS
    try:
        ledger_module.MIGRATIONS = original[:2]
        with Ledger(path) as old:
            old.init()
            assert old.schema_version() == 2
        conn = sqlite3.connect(path)  # apuesta escrita con el esquema v2 (sin columnas nuevas)
        conn.execute(
            "INSERT INTO bets (pot_ids, stake, odds, event, market, selection, bookmaker,"
            " placed_at, status) VALUES ('[1]', 1000, 2.0, 'A vs B', 'h2h', 'A', 'casa',"
            " '2026-11-09T12:00:00+00:00', 'pending')"
        )
        conn.commit()
        conn.close()
    finally:
        ledger_module.MIGRATIONS = original
    with Ledger(path) as new:
        assert new.schema_version() == 3
        [existing] = new.bets()
        assert existing.source == "manual" and existing.stake == 1_000
