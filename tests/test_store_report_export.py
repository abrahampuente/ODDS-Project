"""Tests de capturas/último ranking (store), reporte y exportación."""

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import openpyxl
import pytest

from odds_scanner.client import Event
from odds_scanner.export import (
    BET_COLUMNS,
    POT_COLUMNS,
    RANKING_COLUMNS,
    build_sheets,
    export_csv,
    export_xlsx,
)
from odds_scanner.ledger import Ledger
from odds_scanner.normalize import normalize_events
from odds_scanner.rank import rank, summarize_markets
from odds_scanner.report import build_report, format_report
from odds_scanner.store import (
    closing_best_price,
    latest_capture,
    list_captures,
    load_last_rank,
    save_capture,
    save_last_rank,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
KICKOFF = T0 + timedelta(hours=6)


def event_with(price_home: float, *, books: int = 4) -> Event:
    """Evento de tenis con `books` casas; la casa0 ofrece `price_home` al local."""
    return Event.model_validate(
        {
            "id": "ev-clv",
            "sport_key": "tennis_atp",
            "commence_time": KICKOFF.isoformat(),
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {
                    "key": f"casa{i}",
                    "title": f"casa{i}",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "A", "price": price_home if i == 0 else 1.90},
                                {"name": "B", "price": 1.90},
                            ],
                        }
                    ],
                }
                for i in range(books)
            ],
        }
    )


def sample_events() -> list[Event]:
    raw = json.loads((FIXTURES / "sample_odds_h2h.json").read_text(encoding="utf-8"))
    return [Event.model_validate(e) for e in raw]


# --- capturas -----------------------------------------------------------------


def test_capture_roundtrip_and_latest(tmp_path: Path) -> None:
    assert latest_capture(tmp_path) is None
    first = save_capture(
        tmp_path,
        sample_events(),
        sports=["soccer_epl"],
        markets=["h2h"],
        regions=["eu"],
        captured_at=T0,
    )
    second = save_capture(
        tmp_path,
        [event_with(2.0)],
        sports=["tennis_atp"],
        markets=["h2h"],
        regions=["eu"],
        captured_at=T0 + timedelta(hours=1),
    )
    assert first != second and first.parent == second.parent == tmp_path / "captures"
    captures = list_captures(tmp_path)
    assert [c.captured_at for c in captures] == [T0, T0 + timedelta(hours=1)]
    latest = latest_capture(tmp_path)
    assert latest is not None
    assert latest.sports == ["tennis_atp"]
    assert latest.events[0].id == "ev-clv"
    assert captures[0].events == sample_events()


def test_list_captures_skips_corrupt_files(tmp_path: Path) -> None:
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "capture_roto.json").write_text("{", encoding="utf-8")
    assert list_captures(tmp_path) == []


def test_closing_price_uses_latest_capture_before_kickoff(tmp_path: Path) -> None:
    for hours, price in [(0, 2.10), (3, 2.05), (7, 1.50)]:  # la de +7h es posterior al inicio
        save_capture(
            tmp_path,
            [event_with(price)],
            sports=["tennis_atp"],
            markets=["h2h"],
            regions=["eu"],
            captured_at=T0 + timedelta(hours=hours),
        )
    closing = closing_best_price(
        list_captures(tmp_path),
        event_id="ev-clv",
        market="h2h",
        line=None,
        selection="A",
        commence_time=KICKOFF,
    )
    assert closing == 2.05
    missing = closing_best_price(
        list_captures(tmp_path),
        event_id="otro",
        market="h2h",
        line=None,
        selection="A",
        commence_time=KICKOFF,
    )
    assert missing is None


# --- último ranking -----------------------------------------------------------


def test_last_rank_roundtrip(tmp_path: Path) -> None:
    assert load_last_rank(tmp_path) is None
    rows = rank(summarize_markets(normalize_events(sample_events()).rows), now=T0)
    save_last_rank(tmp_path, rows, view="value", source="fixtures/x.json", created_at=T0)
    loaded = load_last_rank(tmp_path)
    assert loaded is not None
    assert loaded.rows == rows
    assert (loaded.view, loaded.source, loaded.created_at) == ("value", "fixtures/x.json", T0)


# --- reporte ------------------------------------------------------------------


def make_bets(tmp_path: Path) -> Ledger:
    lg = Ledger(tmp_path / "l.db", clock=lambda: T0)
    lg.init()
    common = {"event": "A vs B", "market": "h2h", "bookmaker": "casa0"}
    won = lg.place_bet(
        pot_id=1,
        stake=10_000,
        odds=3.0,
        selection="A",
        book_margin=0.04,
        ev=0.05,
        event_id="ev-clv",
        commence_time=KICKOFF.isoformat(),
        source="row",
        **common,
    )
    lost = lg.place_bet(
        pot_id=2,
        stake=5_000,
        odds=2.0,
        selection="B",
        book_margin=0.08,
        ev=-0.01,
        source="manual",
        **common,
    )
    void = lg.place_bet(pot_id=3, stake=1_000, odds=2.0, selection="B", **common)
    lg.place_bet(pot_id=4, stake=2_000, odds=2.5, selection="A", **common)  # pendiente
    lg.settle(won.id, "won")
    lg.settle(lost.id, "lost")
    lg.settle(void.id, "void")
    return lg


def test_build_report_numbers(tmp_path: Path) -> None:
    with make_bets(tmp_path) as lg:
        report = build_report(lg.state(), lg.bets(), {1: 2.5})
    assert [p.balance for p in report.pots] == [45_000, 20_000, 25_000, 23_000]
    assert report.total_balance == 113_000
    assert report.distance == 387_000
    assert report.progress == pytest.approx(113_000 / 500_000)
    assert (report.won, report.lost, report.void, report.pending) == (1, 1, 1, 1)
    assert report.staked_settled == 15_000  # won + lost (void no cuenta)
    assert report.returned_settled == 30_000
    assert report.profit == 15_000
    assert report.roi == pytest.approx(1.0)
    assert report.avg_book_margin == pytest.approx(0.06)  # solo apuestas con margen conocido
    assert report.avg_ev == pytest.approx(0.02)
    assert [(c.bet_id, c.taken_odds, c.closing_odds) for c in report.clv_rows] == [(1, 3.0, 2.5)]
    assert report.avg_clv == pytest.approx(3.0 / 2.5 - 1)


def test_report_without_settled_bets(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as lg:
        lg.init()
        report = build_report(lg.state(), lg.bets(), {})
    assert report.roi is None and report.avg_book_margin is None and report.avg_clv is None
    text = format_report(report)
    assert "ROI: sin apuestas liquidadas" in text
    assert "CLV: sin capturas de cierre" in text


def test_format_report_contents(tmp_path: Path) -> None:
    with make_bets(tmp_path) as lg:
        text = format_report(build_report(lg.state(), lg.bets(), {1: 2.5}))
    for fragment in (
        "Pozo 1: 45.000",
        "Total: 113.000",
        "Faltan para 500.000: 387.000",
        "Ganadas: 1 | Perdidas: 1 | Nulas: 1 | Pendientes: 1",
        "ROI: +100.00%",
        "Margen promedio pagado: 6.00%",
        "CLV promedio: +20.00%",
        "#1: tomada 3.00 vs cierre 2.50",
    ):
        assert fragment in text


# --- exportación --------------------------------------------------------------


def sheets_for(tmp_path: Path) -> dict[str, tuple[list[str], list[list[object]]]]:
    rows = rank(summarize_markets(normalize_events(sample_events()).rows), now=T0)
    with make_bets(tmp_path) as lg:
        return build_sheets(rows, lg.bets(), lg.pots())


def test_build_sheets_shapes(tmp_path: Path) -> None:
    sheets = sheets_for(tmp_path)
    assert list(sheets) == ["ranking", "apuestas", "pozos"]
    assert sheets["ranking"][0] == RANKING_COLUMNS
    assert sheets["apuestas"][0] == BET_COLUMNS
    assert sheets["pozos"][0] == POT_COLUMNS
    assert len(sheets["ranking"][1]) == 3  # 1 evento h2h con 3 resultados (el otro sin casas)
    assert len(sheets["apuestas"][1]) == 4
    assert len(sheets["pozos"][1]) == 4


def test_export_xlsx_has_sheets_and_columns(tmp_path: Path) -> None:
    path = export_xlsx(tmp_path / "out" / "export.xlsx", sheets_for(tmp_path))
    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == ["ranking", "apuestas", "pozos"]
    for name, columns in (
        ("ranking", RANKING_COLUMNS),
        ("apuestas", BET_COLUMNS),
        ("pozos", POT_COLUMNS),
    ):
        header = [cell.value for cell in workbook[name][1]]
        assert header == columns
    pozos = list(workbook["pozos"].iter_rows(min_row=2, values_only=True))
    assert pozos[0][:3] == (1, 25_000, 45_000)
    assert workbook["ranking"].max_row == 4


def test_export_csv_files(tmp_path: Path) -> None:
    paths = export_csv(tmp_path / "csv", sheets_for(tmp_path))
    assert [p.name for p in paths] == ["ranking.csv", "apuestas.csv", "pozos.csv"]
    with paths[2].open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == POT_COLUMNS
    assert rows[1][:3] == ["1", "25000", "45000"]
