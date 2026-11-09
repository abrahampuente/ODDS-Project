"""Tests de normalización (filas planas) y ranking por margen."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odds_scanner.client import Event
from odds_scanner.math import margin
from odds_scanner.normalize import CHILE_TZ, normalize_events, to_chile
from odds_scanner.rank import RankFilters, rank, summarize_markets

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
REAL_FIXTURE = FIXTURES / "soccer_epl_2026-11-09.json"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


# --- fábrica de eventos sintéticos -------------------------------------------


def outcomes(**prices: float) -> list[dict[str, Any]]:
    return [{"name": name.replace("_", " "), "price": p} for name, p in prices.items()]


def book(key: str, *markets: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "title": key.title(), "markets": list(markets)}


def market(
    key: str, outs: list[dict[str, Any]], updated: str = "2026-11-20T10:00:00Z"
) -> dict[str, Any]:
    return {"key": key, "last_update": updated, "outcomes": outs}


def event(
    *bookmakers: dict[str, Any],
    event_id: str = "ev1",
    sport: str = "soccer_test",
    start: datetime = NOW + timedelta(hours=5),
    home: str = "Local",
    away: str = "Visita",
) -> Event:
    return Event.model_validate(
        {
            "id": event_id,
            "sport_key": sport,
            "commence_time": start.isoformat(),
            "home_team": home,
            "away_team": away,
            "bookmakers": list(bookmakers),
        }
    )


def soccer_h2h(home: float, draw: float, away: float) -> dict[str, Any]:
    return market("h2h", outcomes(Local=home, Draw=draw, Visita=away))


def totals(line: float, over: float, under: float) -> list[dict[str, Any]]:
    return [
        {"name": "Over", "price": over, "point": line},
        {"name": "Under", "price": under, "point": line},
    ]


# --- normalize: filas planas -------------------------------------------------


def test_rows_have_all_fields() -> None:
    ev = event(book("casa_a", soccer_h2h(2.10, 3.40, 3.60)))
    result = normalize_events([ev])
    assert len(result.rows) == 3
    row = result.rows[0]
    assert row.event_id == "ev1"
    assert row.sport == "soccer_test"
    assert row.commence_time_utc == NOW + timedelta(hours=5)
    assert row.commence_time_cl.utcoffset() == timedelta(hours=-3)
    assert (row.home, row.away, row.bookmaker, row.market) == ("Local", "Visita", "casa_a", "h2h")
    assert row.outcome == "Local"
    assert row.point is None and row.line is None
    assert row.price == 2.10
    assert row.last_update == datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    assert result.discarded_total == 0


def test_last_update_falls_back_to_bookmaker() -> None:
    raw_book = book("casa_a", {"key": "h2h", "outcomes": outcomes(Local=2.1, Draw=3.4, Visita=3.6)})
    raw_book["last_update"] = "2026-11-19T09:00:00Z"
    result = normalize_events([event(raw_book)])
    assert result.rows[0].last_update == datetime(2026, 11, 19, 9, 0, tzinfo=UTC)


def test_incomplete_soccer_h2h_is_discarded_and_counted() -> None:
    ev = event(
        book("completa", soccer_h2h(2.10, 3.40, 3.60)),
        book("sin_empate", market("h2h", outcomes(Local=2.0, Visita=3.5))),
    )
    result = normalize_events([ev])
    assert {r.bookmaker for r in result.rows} == {"completa"}
    assert result.discarded == {"incomplete_market": 2}
    assert result.discarded_total == 2


def test_invalid_price_is_discarded_and_makes_market_incomplete() -> None:
    ev = event(
        book("completa", soccer_h2h(2.10, 3.40, 3.60)),
        book("precio_malo", soccer_h2h(2.10, 1.0, 3.60)),
    )
    result = normalize_events([ev])
    assert {r.bookmaker for r in result.rows} == {"completa"}
    assert result.discarded == {"invalid_price": 1, "incomplete_market": 2}


def test_non_soccer_h2h_needs_only_two_outcomes() -> None:
    ev = event(book("casa", market("h2h", outcomes(Local=1.5, Visita=2.6))), sport="tennis_atp")
    assert len(normalize_events([ev]).rows) == 2


def test_lay_markets_are_excluded_and_counted() -> None:
    ev = event(
        book(
            "betfair_ex_eu",
            soccer_h2h(2.10, 3.40, 3.60),
            market("h2h_lay", outcomes(Local=2.14, Draw=3.5, Visita=3.7)),
        )
    )
    result = normalize_events([ev])
    assert {r.market for r in result.rows} == {"h2h"}
    assert result.discarded == {"lay_market": 3}


def test_totals_lines_are_grouped_by_point() -> None:
    ev = event(
        book("casa_a", market("totals", totals(2.5, 1.90, 1.95) + totals(3.5, 2.80, 1.45))),
        book("casa_b", market("totals", totals(2.5, 1.85, 2.00))),
        sport="soccer_test",
    )
    result = normalize_events([ev])
    assert {r.line for r in result.rows} == {2.5, 3.5}
    summaries = {s.line: s for s in summarize_markets(result.rows)}
    assert set(summaries) == {2.5, 3.5}
    over_25 = summaries[2.5].best("Over")
    assert (over_25.price, over_25.bookmaker) == (1.90, "casa_a")  # no 2.80 de la línea 3.5
    assert summaries[2.5].best("Under").price == 2.00
    assert summaries[3.5].n_books == 1
    assert summaries[2.5].best_market_margin == pytest.approx(margin([1.90, 2.00]))


def test_totals_line_missing_under_is_incomplete() -> None:
    lonely_over = [{"name": "Over", "price": 2.8, "point": 3.5}]
    ev = event(book("casa_a", market("totals", totals(2.5, 1.9, 1.95) + lonely_over)))
    result = normalize_events([ev])
    assert {r.line for r in result.rows} == {2.5}
    assert result.discarded == {"incomplete_market": 1}


def test_spreads_line_is_home_perspective() -> None:
    spread = [
        {"name": "Local", "price": 1.95, "point": -1.5},
        {"name": "Visita", "price": 1.90, "point": 1.5},
    ]
    flipped = [
        {"name": "Local", "price": 1.40, "point": 1.5},
        {"name": "Visita", "price": 2.90, "point": -1.5},
    ]
    ev = event(book("casa_a", market("spreads", spread + flipped)), sport="basketball_nba")
    result = normalize_events([ev])
    assert {r.line for r in result.rows} == {-1.5, 1.5}
    summaries = summarize_markets(result.rows)
    assert len(summaries) == 2  # Local -1.5 y Local +1.5 no se mezclan
    by_line = {s.line: s for s in summaries}
    assert by_line[-1.5].best("Visita").point == 1.5


# --- hora de Chile ------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "chile", "offset_h"),
    [
        ("2026-11-21T12:30", "2026-11-21T09:30", -3),  # verano (CLST)
        ("2026-07-01T18:00", "2026-07-01T14:00", -4),  # invierno (CLT)
        ("2026-04-05T02:59", "2026-04-04T23:59", -3),  # justo antes del cambio de abril
        ("2026-04-05T03:00", "2026-04-04T23:00", -4),  # justo después: se atrasa 1 h
        ("2026-09-06T03:59", "2026-09-05T23:59", -4),  # justo antes del cambio de septiembre
        ("2026-09-06T04:00", "2026-09-06T01:00", -3),  # justo después: se adelanta 1 h
    ],
)
def test_to_chile_handles_dst(utc: str, chile: str, offset_h: int) -> None:
    converted = to_chile(datetime.fromisoformat(utc).replace(tzinfo=UTC))
    assert converted.replace(tzinfo=None) == datetime.fromisoformat(chile)
    assert converted.utcoffset() == timedelta(hours=offset_h)
    assert converted.tzinfo == CHILE_TZ


def test_to_chile_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        to_chile(datetime(2026, 11, 21, 12, 30))


# --- rank: evento construido a mano ------------------------------------------


def hand_built_event() -> Event:
    # casa_a: 2.10 / 3.40 / 3.60  -> margen 0.048085
    # casa_b: 2.00 / 3.50 / 3.80  -> margen 0.048872
    # mejores: Local 2.10 (a), Draw 3.50 (b), Visita 3.80 (b) -> margen 0.025063
    return event(
        book("casa_a", soccer_h2h(2.10, 3.40, 3.60)),
        book("casa_b", soccer_h2h(2.00, 3.50, 3.80)),
    )


def test_hand_built_event_margins_and_best_books() -> None:
    [summary] = summarize_markets(normalize_events([hand_built_event()]).rows)
    assert summary.margin_by_book["casa_a"] == pytest.approx(0.048085, abs=1e-6)
    assert summary.margin_by_book["casa_b"] == pytest.approx(0.048872, abs=1e-6)
    assert (summary.best("Local").price, summary.best("Local").bookmaker) == (2.10, "casa_a")
    assert (summary.best("Draw").price, summary.best("Draw").bookmaker) == (3.50, "casa_b")
    assert (summary.best("Visita").price, summary.best("Visita").bookmaker) == (3.80, "casa_b")
    assert summary.best_market_margin == pytest.approx(0.025063, abs=1e-6)
    assert summary.best_market_margin == pytest.approx(margin([2.10, 3.50, 3.80]))
    assert summary.is_arbitrage is False
    assert summary.n_books == 2


def test_best_price_tie_is_deterministic() -> None:
    ev = event(book("zeta", soccer_h2h(2.1, 3.4, 3.6)), book("alfa", soccer_h2h(2.1, 3.3, 3.5)))
    [summary] = summarize_markets(normalize_events([ev]).rows)
    assert summary.best("Local").bookmaker == "alfa"


def test_ranked_rows_carry_book_margin() -> None:
    ranked = rank(summarize_markets(normalize_events([hand_built_event()]).rows), now=NOW)
    by_outcome = {r.outcome: r for r in ranked}
    assert by_outcome["Local"].book_margin == pytest.approx(margin([2.10, 3.40, 3.60]))
    assert by_outcome["Draw"].book_margin == pytest.approx(margin([2.00, 3.50, 3.80]))
    assert all(r.best_market_margin == pytest.approx(0.025063, abs=1e-6) for r in ranked)


# --- arbitraje ----------------------------------------------------------------


def load_events(path: Path) -> list[Event]:
    return [Event.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))]


def test_arbitrage_detected_in_synthetic_fixture() -> None:
    events = load_events(FIXTURES / "synthetic_arbitrage.json")
    [summary] = summarize_markets(normalize_events(events).rows)
    assert summary.best_market_margin == pytest.approx(1 / 2.20 + 1 / 2.10 - 1)
    assert summary.best_market_margin < 0
    assert summary.is_arbitrage is True
    assert (summary.best("Jugador A").bookmaker, summary.best("Jugador B").bookmaker) == (
        "casa_uno",
        "casa_dos",
    )
    ranked = rank([summary], now=NOW)
    assert all(r.is_arbitrage for r in ranked)


# --- ranking y filtros --------------------------------------------------------


def three_events() -> list[Event]:
    return [
        event(
            book("a", soccer_h2h(1.95, 3.60, 4.20)), event_id="low", start=NOW + timedelta(hours=2)
        ),
        event(
            book("a", soccer_h2h(2.10, 3.40, 3.60)), event_id="mid", start=NOW + timedelta(hours=30)
        ),
        event(
            book("a", soccer_h2h(1.80, 3.20, 4.80)),
            event_id="high",
            sport="soccer_other",
            start=NOW + timedelta(hours=10),
        ),
    ]


def test_rank_sorted_by_best_market_margin_ascending() -> None:
    summaries = summarize_markets(normalize_events(three_events()).rows)
    ranked = rank(summaries, now=NOW)
    margins = [r.best_market_margin for r in ranked]
    assert margins == sorted(margins)
    assert len(ranked) == 9


def test_filter_by_odds_range() -> None:
    summaries = summarize_markets(normalize_events(three_events()).rows)
    ranked = rank(summaries, RankFilters(min_odds=4.0, max_odds=5.5), now=NOW)
    assert sorted((r.event_id, r.outcome, r.best_price) for r in ranked) == [
        ("high", "Visita", 4.80),
        ("low", "Visita", 4.20),
    ]


def test_filter_by_hours_window() -> None:
    summaries = summarize_markets(normalize_events(three_events()).rows)
    ranked = rank(summaries, RankFilters(within_hours=24), now=NOW)
    assert {r.event_id for r in ranked} == {"low", "high"}
    ranked = rank(summaries, RankFilters(within_hours=5), now=NOW)
    assert {r.event_id for r in ranked} == {"low"}


def test_started_events_are_excluded_by_hours_window() -> None:
    summaries = summarize_markets(normalize_events(three_events()).rows)
    later = NOW + timedelta(hours=3)  # "low" ya empezó
    assert "low" not in {
        r.event_id for r in rank(summaries, RankFilters(within_hours=48), now=later)
    }


def test_filter_by_sport_and_market() -> None:
    summaries = summarize_markets(normalize_events(three_events()).rows)
    assert {r.event_id for r in rank(summaries, RankFilters(sports={"soccer_other"}), now=NOW)} == {
        "high"
    }
    assert rank(summaries, RankFilters(markets={"totals"}), now=NOW) == []


def test_filters_validate_ranges() -> None:
    with pytest.raises(ValueError):
        RankFilters(min_odds=5.0, max_odds=4.0)
    with pytest.raises(ValueError):
        RankFilters(within_hours=-1)


# --- fixture real (fase 3) ----------------------------------------------------


def test_real_fixture_normalizes_and_ranks() -> None:
    events = load_events(REAL_FIXTURE)
    result = normalize_events(events)
    assert result.rows
    assert result.discarded.get("lay_market", 0) == 27  # 9 mercados h2h_lay x 3
    assert {r.market for r in result.rows} == {"h2h"}
    summaries = summarize_markets(result.rows)
    assert len(summaries) == len(events)  # un mercado h2h por evento
    for s in summaries:
        assert len(s.best_prices) == 3
        assert s.best_market_margin == pytest.approx(margin([b.price for b in s.best_prices]))
    ranked = rank(summaries, now=datetime(2026, 11, 9, 17, 0, tzinfo=UTC))
    margins = [r.best_market_margin for r in ranked]
    assert margins == sorted(margins)
