"""Tests de los comandos `odds-scanner ledger ...`."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from odds_scanner.cli import app

runner = CliRunner()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def run(db: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["ledger", "--db", str(db), *args])
    return result.exit_code, result.output


# Fase 7: las apuestas manuales sin --market-odds tienen margen desconocido y piden
# confirmación; en modo no interactivo eso exige --yes y --accept-high-margin.
BET_ARGS = [
    "--event",
    "Chelsea vs Leeds",
    "--market",
    "h2h",
    "--bookmaker",
    "pinnacle",
    "--yes",
    "--accept-high-margin",
]


def test_init_status_and_repeat(db: Path) -> None:
    code, out = run(db, "init")
    assert code == 0, out
    assert "100.000" in out
    code, out = run(db, "init")
    assert code == 1 and "--reset --confirm" in out
    code, out = run(db, "init", "--reset")
    assert code == 1 and "--confirm" in out
    code, out = run(db, "init", "--reset", "--confirm")
    assert code == 0, out
    code, out = run(db, "status")
    assert code == 0 and "EN CURSO" in out and "25.000" in out


def test_default_db_is_data_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ledger", "init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "ledger.db").is_file()


def test_full_flow_to_goal(db: Path) -> None:
    run(db, "init")
    code, out = run(
        db,
        "bet",
        "--pot",
        "1",
        "--stake",
        "25000",
        "--odds",
        "4.5",
        "--selection",
        "Leeds",
        *BET_ARGS,
    )
    assert code == 0, out
    assert "Apuesta #1" in out
    code, out = run(db, "settle", "1", "won")
    assert code == 0 and "112.500" in out
    code, out = run(db, "merge", "1", "2", "3", "4")
    assert code == 0 and "187.500" in out
    code, out = run(db, "plan")
    assert code == 0 and "2.67" in out and "2.6667" in out
    run(
        db,
        "bet",
        "--pot",
        "1",
        "--stake",
        "187500",
        "--odds",
        "2.67",
        "--selection",
        "Chelsea",
        *BET_ARGS,
    )
    code, out = run(db, "settle", "2", "won")
    assert code == 0 and "META CUMPLIDA" in out
    code, out = run(
        db, "bet", "--pot", "1", "--stake", "1000", "--odds", "2", "--selection", "X", *BET_ARGS
    )
    assert code == 1 and "META CUMPLIDA" in out
    code, out = run(db, "log")
    assert code == 0
    assert [a for a in ("init", "bet", "settle", "merge") if a in out] == [
        "init",
        "bet",
        "settle",
        "merge",
    ]


def test_budget_exhausted_offers_nothing(db: Path) -> None:
    run(db, "init")
    for pot in range(1, 5):
        run(
            db,
            "bet",
            "--pot",
            str(pot),
            "--stake",
            "25000",
            "--odds",
            "3",
            "--selection",
            "X",
            *BET_ARGS,
        )
        run(db, "settle", str(pot), "lost")
    code, out = run(db, "status")
    assert code == 0 and "PRESUPUESTO AGOTADO" in out
    assert "Siguiente paso" not in out
    code, out = run(db, "plan")
    assert code == 0 and "no se permiten recargas" in out
    assert "cuota" not in out.lower().split("no se permiten recargas")[1]


def test_rule_violations_exit_1(db: Path) -> None:
    run(db, "init")
    code, out = run(
        db, "bet", "--pot", "1", "--stake", "30000", "--odds", "2", "--selection", "X", *BET_ARGS
    )
    assert code == 1 and "saldo" in out
    code, out = run(db, "settle", "99", "won")
    assert code == 1 and "no existe" in out
    code, out = run(db, "settle", "1", "maybe")
    assert code != 0
    code, out = run(db, "merge", "1")
    assert code == 1


def test_status_lists_pending_bets(db: Path) -> None:
    run(db, "init")
    run(
        db,
        "bet",
        "--pot",
        "2",
        "--stake",
        "5000",
        "--odds",
        "3.1",
        "--selection",
        "Draw",
        *BET_ARGS,
    )
    code, out = run(db, "status")
    assert code == 0
    assert "Pendientes" in out and "Draw" in out and "5.000" in out
    assert "Siguiente paso" in out


def test_status_before_init(db: Path) -> None:
    code, out = run(db, "status")
    assert code == 0 and "SIN INICIAR" in out
