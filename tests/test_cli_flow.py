"""Tests de la CLI completa (fase 7): scan, rank/value, bet con guardas, export y report."""

import csv
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import openpyxl
import pytest
import respx
from typer.testing import CliRunner

from odds_scanner import timeutil
from odds_scanner.cli import app
from odds_scanner.client import BASE_URL
from odds_scanner.ledger import Ledger
from odds_scanner.store import latest_capture, load_last_rank
from tests.conftest import FAKE_KEY

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
REAL = FIXTURES / "soccer_epl_2026-11-09.json"
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)
EPL = f"{BASE_URL}/v4/sports/soccer_epl/odds"
LALIGA = f"{BASE_URL}/v4/sports/soccer_spain_la_liga/odds"
QUOTA_HEADERS = {"x-requests-remaining": "496", "x-requests-used": "4", "x-requests-last": "2"}


@pytest.fixture
def flow(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Directorio de trabajo con .env, reloj fijo y ledger inicializado."""
    (workdir / ".env").write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(timeutil, "utcnow", lambda: NOW)
    assert runner.invoke(app, ["init"]).exit_code == 0
    yield workdir


def invoke(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


def seed_quota(workdir: Path, remaining: int) -> None:
    (workdir / "data" / "quota.json").write_text(
        json.dumps({"remaining": remaining, "used": 0, "last": 0, "updated_at": "x"}),
        encoding="utf-8",
    )


def sample_odds() -> object:
    return json.loads((FIXTURES / "sample_odds_h2h.json").read_text(encoding="utf-8"))


SCAN = ["scan", "--sports", "soccer_epl,soccer_spain_la_liga", "--markets", "h2h,totals"]


# --- ayuda en español ---------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "phrase"),
    [
        ("scan", "costo estimado"),
        ("rank", "margen combinado"),
        ("value", "EV"),
        ("bet", "fila del ranking"),
        ("settle", "Liquida"),
        ("merge", "Junta"),
        ("plan", "cuota necesaria"),
        ("status", "Estado global"),
        ("export", "hojas"),
        ("report", "Resumen"),
        ("init", "pozos"),
    ],
)
def test_every_command_has_spanish_help(command: str, phrase: str) -> None:
    code, out = invoke(command, "--help")
    assert code == 0
    assert phrase.lower() in " ".join(out.split()).lower()


# --- scan ---------------------------------------------------------------------


def test_scan_dry_run_shows_cost_without_network(flow: Path) -> None:
    seed_quota(flow, 500)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)
        code, out = invoke(*SCAN, "--dry-run")
    assert code == 0, out
    assert "Costo estimado: 4 consultas" in out
    assert "soccer_epl: 2" in out and "soccer_spain_la_liga: 2" in out
    assert "quedarían 496" in out
    assert "dry-run" in out
    assert not route.called


def test_scan_declined_does_not_call_api(flow: Path) -> None:
    seed_quota(flow, 500)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)
        code, out = invoke(*SCAN, input="n\n")
    assert code == 1
    assert "Cancelado" in out and "[y/N]" in out
    assert not route.called


def test_scan_confirmed_calls_saves_capture_and_ranks(flow: Path) -> None:
    seed_quota(flow, 500)
    with respx.mock:
        epl = respx.get(EPL).respond(200, json=sample_odds(), headers=QUOTA_HEADERS)
        liga = respx.get(LALIGA).respond(200, json=[], headers=QUOTA_HEADERS)
        code, out = invoke(*SCAN, input="y\n")
    assert code == 0, out
    assert epl.called and liga.called
    assert epl.calls.last.request.url.params["markets"] == "h2h,totals"
    capture = latest_capture(flow / "data")
    assert capture is not None
    assert capture.sports == ["soccer_epl", "soccer_spain_la_liga"]
    assert len(capture.events) == 2
    assert "Arsenal - Chelsea" in out
    last = load_last_rank(flow / "data")
    assert last is not None and last.source.startswith("captura")
    assert FAKE_KEY not in out


def test_scan_yes_skips_prompt_and_applies_filters(flow: Path) -> None:
    seed_quota(flow, 500)
    with respx.mock:
        respx.get(EPL).respond(200, json=sample_odds(), headers=QUOTA_HEADERS)
        code, out = invoke(
            "scan",
            "--sports",
            "soccer_epl",
            "--markets",
            "h2h",
            "--hours",
            "200",
            "--odds-min",
            "3.5",
            "--odds-max",
            "5.5",
            "--yes",
        )
    assert code == 0, out
    assert "[y/N]" not in out
    last = load_last_rank(flow / "data")
    assert last is not None
    assert {r.outcome for r in last.rows} == {
        "Chelsea",
        "Draw",
    }  # mejores 3.75 y 3.50; Arsenal 2.14 queda fuera


def test_scan_blocked_by_quota_reserve_before_prompt(flow: Path) -> None:
    seed_quota(flow, 52)  # 52 - 4 = 48 < 50
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)
        code, out = invoke(*SCAN, input="y\n")
    assert code == 1
    assert "reserva" in out.lower() and "[y/N]" not in out
    assert not route.called


def test_scan_requires_known_quota(flow: Path) -> None:
    code, out = invoke(*SCAN, "--yes")
    assert code == 1 and "odds-scanner sports" in out


def test_scan_rejects_bad_markets(flow: Path) -> None:
    code, out = invoke("scan", "--sports", "soccer_epl", "--markets", " ", "--dry-run")
    assert code == 1 and "mercado" in out


# --- rank / value -------------------------------------------------------------


def test_rank_without_source_fails(flow: Path) -> None:
    code, out = invoke("rank")
    assert code == 1 and "scan" in out and "--from-fixture" in out


def test_rank_uses_latest_capture(flow: Path) -> None:
    seed_quota(flow, 500)
    with respx.mock:
        respx.get(EPL).respond(200, json=sample_odds(), headers=QUOTA_HEADERS)
        invoke("scan", "--sports", "soccer_epl", "--markets", "h2h", "--yes")
    code, out = invoke("rank")
    assert code == 0, out
    assert "Fuente: captura" in out and "Arsenal - Chelsea" in out


def test_value_command_and_odds_aliases(flow: Path) -> None:
    code, out = invoke("value", "--from-fixture", str(REAL), "--odds-min", "4", "--odds-max", "5.5")
    assert code == 0, out
    assert "por EV" in out
    last = load_last_rank(flow / "data")
    assert last is not None and last.view == "value"
    assert all(4 <= r.best_price <= 5.5 for r in last.rows)


# --- bet: guardas -------------------------------------------------------------


def rank_fixture() -> None:
    code, out = invoke("rank", "--from-fixture", str(REAL))
    assert code == 0, out


def test_bet_from_row_shows_summary_and_registers(flow: Path) -> None:
    rank_fixture()
    last = load_last_rank(flow / "data")
    assert last is not None
    row = last.rows[0]
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "10000", input="y\n")
    assert code == 0, out
    for fragment in (
        "Pozo: 1",
        "Stake: 10.000",
        f"Cuota: {row.best_price:.2f}",
        "Ganancia posible:",
        "Cuota necesaria según plan (pozo 1): 20.00",
        "¿Llega a 500.000 si gana?: no",
        "[y/N]",
        "Apuesta #1 registrada",
    ):
        assert fragment in out, fragment
    with Ledger(flow / "data" / "ledger.db") as lg:
        [bet] = lg.bets()
    assert (bet.odds, bet.bookmaker, bet.selection) == (row.best_price, row.bookmaker, row.outcome)
    assert bet.source == "row" and bet.event_id == row.event_id
    assert bet.book_margin == pytest.approx(row.book_margin)
    assert bet.ev == row.ev


def test_bet_summary_says_goal_reachable(flow: Path) -> None:
    code, out = invoke(
        "bet",
        "--pot",
        "1",
        "--stake",
        "25000",
        "--odds",
        "20",
        "--event",
        "X vs Y",
        "--market",
        "h2h",
        "--selection",
        "X",
        "--bookmaker",
        "casa",
        "--market-odds",
        "20,1.04",
        "--yes",
    )
    assert code == 0, out
    assert "¿Llega a 500.000 si gana?: sí" in out


def test_bet_declined_registers_nothing(flow: Path) -> None:
    rank_fixture()
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "10000", input="n\n")
    assert code == 1 and "Cancelado" in out
    with Ledger(flow / "data" / "ledger.db") as lg:
        assert lg.bets() == [] and lg.pot(1).balance == 25_000


def test_bet_row_errors(flow: Path) -> None:
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "100")
    assert code == 1 and "rank" in out  # no hay ranking guardado
    rank_fixture()
    code, out = invoke("bet", "--row", "999", "--pot", "1", "--stake", "100")
    assert code == 1 and "999" in out
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "100", "--odds", "3")
    assert code == 1 and "--row" in out


def test_bet_row_rejects_started_event(flow: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rank_fixture()
    monkeypatch.setattr(timeutil, "utcnow", lambda: datetime(2026, 12, 1, tzinfo=UTC))
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "100", "--yes")
    assert code == 1 and "ya empezó" in out


def test_bet_validates_rules_before_asking(flow: Path) -> None:
    rank_fixture()
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "30000", input="y\n")
    assert code == 1 and "saldo" in out and "[y/N]" not in out


MANUAL = ["--event", "X vs Y", "--market", "h2h", "--selection", "X", "--bookmaker", "casa"]


def test_manual_bet_requires_all_fields(flow: Path) -> None:
    code, out = invoke("bet", "--pot", "1", "--stake", "100", "--odds", "2")
    assert code == 1 and "--event" in out and "--row" in out


def test_manual_bet_low_margin_needs_single_confirmation(flow: Path) -> None:
    code, out = invoke(
        "bet",
        "--pot",
        "1",
        "--stake",
        "1000",
        "--odds",
        "2.10",
        *MANUAL,
        "--market-odds",
        "2.10,3.40,3.60",
        input="y\n",
    )
    assert code == 0, out
    assert "Margen de la casa: 4.81%" in out
    assert out.count("[y/N]") == 1


def test_manual_bet_high_margin_asks_extra_confirmation(flow: Path) -> None:
    args = [
        "bet",
        "--pot",
        "1",
        "--stake",
        "1000",
        "--odds",
        "1.80",
        *MANUAL,
        "--market-odds",
        "1.80,3.20,3.80",
    ]
    code, out = invoke(*args, input="n\n")
    assert code == 1 and "13.12%" in out and "> 7%" in out
    with Ledger(flow / "data" / "ledger.db") as lg:
        assert lg.bets() == []
    code, out = invoke(*args, input="y\ny\n")
    assert code == 0, out
    assert out.count("[y/N]") == 2


def test_manual_bet_unknown_margin_asks_extra_confirmation(flow: Path) -> None:
    code, out = invoke(
        "bet", "--pot", "1", "--stake", "1000", "--odds", "2.5", *MANUAL, input="y\ny\n"
    )
    assert code == 0, out
    assert "margen desconocido" in out.lower()


def test_manual_high_margin_with_yes_requires_explicit_flag(flow: Path) -> None:
    args = [
        "bet",
        "--pot",
        "1",
        "--stake",
        "1000",
        "--odds",
        "1.80",
        *MANUAL,
        "--market-odds",
        "1.80,3.20,3.80",
        "--yes",
    ]
    code, out = invoke(*args)
    assert code == 1 and "--accept-high-margin" in out
    code, out = invoke(*args, "--accept-high-margin")
    assert code == 0, out


def test_manual_market_odds_must_include_odds(flow: Path) -> None:
    code, out = invoke(
        "bet",
        "--pot",
        "1",
        "--stake",
        "1000",
        "--odds",
        "2.5",
        *MANUAL,
        "--market-odds",
        "2.10,3.40",
        "--yes",
    )
    assert code == 1 and "--market-odds" in out


# --- settle / merge / plan / status (primer nivel) ----------------------------


def test_top_level_ledger_commands(flow: Path) -> None:
    code, out = invoke(
        "bet",
        "--pot",
        "1",
        "--stake",
        "25000",
        "--odds",
        "4.5",
        *MANUAL,
        "--market-odds",
        "4.5,1.28",
        "--yes",
    )
    assert code == 0, out
    code, out = invoke("settle", "1", "won")
    assert code == 0 and "112.500" in out
    code, out = invoke("merge", "1", "2", "3", "4")
    assert code == 0 and "187.500" in out
    code, out = invoke("plan")
    assert code == 0 and "2.67" in out
    code, out = invoke("status")
    assert code == 0 and "EN CURSO" in out


# --- export / report ----------------------------------------------------------


def test_export_xlsx_and_csv(flow: Path) -> None:
    rank_fixture()
    invoke("bet", "--row", "1", "--pot", "1", "--stake", "1000", "--yes")
    code, out = invoke("export", "--format", "xlsx", "--out", "data/exports/x.xlsx")
    assert code == 0, out
    workbook = openpyxl.load_workbook(flow / "data" / "exports" / "x.xlsx")
    assert workbook.sheetnames == ["ranking", "apuestas", "pozos"]
    assert workbook["ranking"].max_row == 61  # 60 resultados + encabezado
    assert [c.value for c in workbook["apuestas"][1]][:4] == ["id", "pozo", "stake", "cuota"]
    code, out = invoke("export", "--format", "csv", "--out", "data/exports/csv")
    assert code == 0, out
    with (flow / "data" / "exports" / "csv" / "ranking.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        rows = list(csv.reader(f))
    assert len(rows) == 61 and rows[0][0] == "posicion"


def test_export_default_path_and_missing_ranking(flow: Path) -> None:
    code, out = invoke("export", "--format", "xlsx")
    assert code == 0, out
    assert "sin ranking guardado" in out
    assert list((flow / "data" / "exports").glob("export_*.xlsx"))


def test_report_command(flow: Path) -> None:
    code, out = invoke(
        "bet",
        "--pot",
        "1",
        "--stake",
        "10000",
        "--odds",
        "3",
        *MANUAL,
        "--market-odds",
        "3,1.45",
        "--yes",
    )
    assert code == 0, out
    invoke("settle", "1", "won")
    code, out = invoke("report")
    assert code == 0, out
    for fragment in (
        "Total: 120.000",
        "Faltan para 500.000: 380.000",
        "Ganadas: 1",
        "ROI: +200.00%",
    ):
        assert fragment in out, fragment


def test_report_shows_clv_from_capture(flow: Path) -> None:
    from odds_scanner.cli import _load_fixture_events
    from odds_scanner.store import save_capture

    events = _load_fixture_events(REAL)
    save_capture(
        flow / "data",
        events,
        sports=["soccer_epl"],
        markets=["h2h"],
        regions=["eu"],
        captured_at=NOW,  # antes del inicio de todos los partidos
    )
    rank_fixture()
    code, out = invoke("bet", "--row", "1", "--pot", "1", "--stake", "1000", "--yes")
    assert code == 0, out
    code, out = invoke("report")
    assert code == 0, out
    assert "CLV promedio: +0.00% (1 apuestas)" in out
    assert "#1: tomada 1.94 vs cierre 1.94" in out
