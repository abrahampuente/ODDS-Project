"""Tests de comisiones de exchanges y su efecto en normalización y ranking."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odds_scanner.cli import app
from odds_scanner.client import Event
from odds_scanner.config import ConfigError, load_settings
from odds_scanner.exchanges import DEFAULT_COMMISSIONS, parse_commissions, resolve_commissions
from odds_scanner.normalize import normalize_events
from odds_scanner.rank import rank, summarize_markets

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
REAL_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "soccer_epl_2026-11-09.json"


def tennis(books: dict[str, tuple[float, float]]) -> Event:
    return Event.model_validate(
        {
            "id": "t1",
            "sport_key": "tennis_atp",
            "commence_time": (NOW + timedelta(hours=3)).isoformat(),
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {
                    "key": key,
                    "title": key,
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [{"name": "A", "price": a}, {"name": "B", "price": b}],
                        }
                    ],
                }
                for key, (a, b) in books.items()
            ],
        }
    )


# --- parseo -------------------------------------------------------------------


def test_parse_commissions_accepts_fraction_and_percent() -> None:
    assert parse_commissions("betfair_ex_eu:0.03, matchbook:1.5%") == {
        "betfair_ex_eu": pytest.approx(0.03),
        "matchbook": pytest.approx(0.015),
    }
    assert parse_commissions("") == {}
    assert parse_commissions(" , ") == {}


@pytest.mark.parametrize("spec", ["betfair_ex_eu", "betfair_ex_eu:", ":0.05", "x:abc", "x:1.2"])
def test_parse_commissions_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_commissions(spec)


def test_resolve_commissions_overrides_defaults() -> None:
    resolved = resolve_commissions("betfair_ex_eu:0.02,otra_ex:0.01")
    assert resolved["betfair_ex_eu"] == pytest.approx(0.02)
    assert resolved["otra_ex"] == pytest.approx(0.01)
    assert resolved["matchbook"] == DEFAULT_COMMISSIONS["matchbook"]


def test_settings_commissions_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXCHANGE_COMMISSIONS=betfair_ex_eu:0.03\n", encoding="utf-8")
    assert load_settings(env_file=env).commissions()["betfair_ex_eu"] == pytest.approx(0.03)
    env.write_text("EXCHANGE_COMMISSIONS=betfair_ex_eu\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="EXCHANGE_COMMISSIONS"):
        load_settings(env_file=env).commissions()


# --- normalización ------------------------------------------------------------


def test_exchange_prices_are_net_of_commission() -> None:
    rows = normalize_events([tennis({"betfair_ex_eu": (2.50, 1.60), "casa": (2.40, 1.55)})]).rows
    by = {(r.bookmaker, r.outcome): r for r in rows}
    bf = by[("betfair_ex_eu", "A")]
    assert bf.gross_price == 2.50
    assert bf.commission == pytest.approx(0.05)
    assert bf.price == pytest.approx(1 + 1.5 * 0.95)  # 2.425
    plain = by[("casa", "A")]
    assert (plain.price, plain.gross_price, plain.commission) == (2.40, 2.40, 0.0)


def test_commission_zero_keeps_gross_price() -> None:
    rows = normalize_events([tennis({"betfair_ex_eu": (2.50, 1.60)})], {}).rows
    assert all(r.price == r.gross_price and r.commission == 0.0 for r in rows)


def test_gross_exchange_price_no_longer_wins_after_commission() -> None:
    """Bruta 2.50 en Betfair parece mejor que 2.45, pero neta (2.425) no lo es."""
    ev = tennis({"betfair_ex_eu": (2.50, 1.60), "casa": (2.45, 1.55)})
    summary = summarize_markets(normalize_events([ev]).rows)[0]
    best_a = summary.best("A")
    assert (best_a.bookmaker, best_a.price, best_a.gross_price) == ("casa", 2.45, 2.45)
    gross_view = summarize_markets(normalize_events([ev], {}).rows)[0]
    assert gross_view.best("A").bookmaker == "betfair_ex_eu"


def test_false_arbitrage_from_gross_exchange_odds_disappears() -> None:
    """Con cuotas brutas el mercado combinado da margen negativo; neto ya no."""
    ev = tennis({"betfair_ex_eu": (2.04, 2.04), "casa": (1.95, 1.90)})
    gross = summarize_markets(normalize_events([ev], {}).rows)[0]
    net = summarize_markets(normalize_events([ev]).rows)[0]
    assert gross.is_arbitrage is True
    assert net.is_arbitrage is False


def test_ranked_outcome_carries_gross_price_and_commission() -> None:
    ev = tennis({"betfair_ex_eu": (3.00, 1.45), "casa": (2.60, 1.50)})
    rows = rank(summarize_markets(normalize_events([ev]).rows), now=NOW)
    a = next(r for r in rows if r.outcome == "A")
    assert a.bookmaker == "betfair_ex_eu"
    assert a.best_price == pytest.approx(2.90)
    assert (a.gross_price, a.commission) == (3.00, pytest.approx(0.05))


def test_real_fixture_false_arbitrages_came_from_betfair_gross_odds() -> None:
    """En la captura real, 3 de 5 "arbitrajes" eran la cuota bruta de Betfair."""
    raw = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    events = [Event.model_validate(e) for e in raw]

    def arbitrages(commissions: dict[str, float]) -> set[str]:
        rows = normalize_events(events, commissions).rows
        return {f"{s.home} - {s.away}" for s in summarize_markets(rows) if s.is_arbitrage}

    gross = arbitrages({})
    net = arbitrages(dict(DEFAULT_COMMISSIONS))
    assert "Chelsea - Leeds United" in gross
    assert "Chelsea - Leeds United" not in net
    assert net == {"Bournemouth - Nottingham Forest", "Brentford - Everton"}
    assert len(gross) == 5


def test_rank_cli_marks_exchange_and_explains_commission(workdir: Path, tmp_path: Path) -> None:
    ev = tennis({"betfair_ex_eu": (3.00, 1.45), "casa": (2.60, 1.50)})
    fixture = tmp_path / "ex.json"
    fixture.write_text(json.dumps([ev.model_dump(mode="json")]), encoding="utf-8")
    result = CliRunner().invoke(app, ["rank", "--from-fixture", str(fixture)])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if " 2.90 " in line)
    assert "*betfair" in row
    assert "descuenta la comisión (betfair_ex_eu 5.0%)" in result.output


def test_rank_cli_rejects_bad_commission_config(workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("EXCHANGE_COMMISSIONS=betfair_ex_eu\n", encoding="utf-8")
    ev = tennis({"casa": (2.60, 1.50)})
    fixture = tmp_path / "ex.json"
    fixture.write_text(json.dumps([ev.model_dump(mode="json")]), encoding="utf-8")
    result = CliRunner().invoke(app, ["rank", "--from-fixture", str(fixture)])
    assert result.exit_code == 1
    assert "EXCHANGE_COMMISSIONS" in result.output
