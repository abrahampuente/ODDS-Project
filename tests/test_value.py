"""Tests de probabilidad justa por consenso y EV."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odds_scanner.client import Event
from odds_scanner.math import fair_probs, fair_probs_power, overround
from odds_scanner.normalize import OddsRow, normalize_events
from odds_scanner.rank import RankFilters, rank, summarize_markets
from odds_scanner.value import (
    DEFAULT_DEVIG,
    INSUFFICIENT_DATA,
    MIN_BOOKS,
    consensus_fair_prob,
    label_ev,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def tennis_event(books: dict[str, tuple[float, float]], event_id: str = "t1") -> Event:
    return Event.model_validate(
        {
            "id": event_id,
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


def soccer_event(books: dict[str, tuple[float, float, float]]) -> Event:
    raw: dict[str, Any] = {
        "id": "s1",
        "sport_key": "soccer_test",
        "commence_time": (NOW + timedelta(hours=3)).isoformat(),
        "home_team": "L",
        "away_team": "V",
        "bookmakers": [
            {
                "key": key,
                "title": key,
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "L", "price": h},
                            {"name": "Draw", "price": d},
                            {"name": "V", "price": a},
                        ],
                    }
                ],
            }
            for key, (h, d, a) in books.items()
        ],
    }
    return Event.model_validate(raw)


def rows_of(*events: Event) -> list[OddsRow]:
    return normalize_events(events).rows


# Método proporcional (devig="proportional"):
# 4 casas con 1.90/1.90 (justa 0.5/0.5) + 1 casa con 2.20/1.65 (justa 3/7, 4/7).
# Consenso: A = (4*0.5 + 3/7)/5 = 17/35 ; B = 18/35.
# Mejor A = 2.20 -> EV = 2.2*17/35 - 1 = 2.4/35 = +6.857% (revisar)
# Mejor B = 1.90 -> EV = 1.9*18/35 - 1 = -0.8/35 = -2.286% (evitar)
FIVE_BOOKS = {
    "b1": (1.90, 1.90),
    "b2": (1.90, 1.90),
    "b3": (1.90, 1.90),
    "b4": (1.90, 1.90),
    "outlier": (2.20, 1.65),
}


# --- consenso -----------------------------------------------------------------


def test_consensus_five_books_known_values() -> None:
    result = consensus_fair_prob(
        rows_of(tennis_event(FIVE_BOOKS)), "t1", "h2h", devig="proportional"
    )
    assert result.n_books == 5
    assert result.sufficient is True
    assert result.fair_probs is not None
    assert result.fair_probs["A"] == pytest.approx(17 / 35)
    assert result.fair_probs["B"] == pytest.approx(18 / 35)


def test_ev_five_books_known_values() -> None:
    summaries = summarize_markets(rows_of(tennis_event(FIVE_BOOKS)), devig="proportional")
    by_outcome = {r.outcome: r for r in rank(summaries, now=NOW)}
    a, b = by_outcome["A"], by_outcome["B"]
    assert (a.best_price, a.bookmaker) == (2.20, "outlier")
    assert a.fair_prob == pytest.approx(17 / 35)
    assert a.ev == pytest.approx(2.4 / 35)
    assert a.label == "revisar"
    assert (b.best_price, b.bookmaker) == (1.90, "b1")
    assert b.ev == pytest.approx(-0.8 / 35)
    assert b.label == "evitar"
    assert a.n_books == b.n_books == 5


def test_fewer_than_min_books_has_no_ev() -> None:
    three = {k: FIVE_BOOKS[k] for k in ("b1", "b2", "outlier")}
    result = consensus_fair_prob(rows_of(tennis_event(three)), "t1", "h2h")
    assert MIN_BOOKS == 4
    assert result.n_books == 3
    assert result.sufficient is False
    assert result.fair_probs is None
    ranked = rank(summarize_markets(rows_of(tennis_event(three))), now=NOW)
    assert all(r.ev is None and r.fair_prob is None for r in ranked)
    assert all(r.label == INSUFFICIENT_DATA == "datos insuficientes" for r in ranked)
    assert all(r.n_books == 3 for r in ranked)


def test_exactly_four_books_is_enough() -> None:
    four = {k: FIVE_BOOKS[k] for k in ("b1", "b2", "b3", "outlier")}
    assert consensus_fair_prob(rows_of(tennis_event(four)), "t1", "h2h").sufficient is True


def test_identical_books_ev_equals_inverse_overround_minus_one() -> None:
    odds = (2.10, 3.40, 3.60)
    event = soccer_event({f"casa{i}": odds for i in range(5)})
    expected = 1 / overround(list(odds)) - 1
    ranked = rank(summarize_markets(rows_of(event), devig="proportional"), now=NOW)
    assert len(ranked) == 3
    for row in ranked:
        assert row.ev == pytest.approx(expected)
        assert row.ev is not None and row.ev < 0
        assert row.label == "evitar"


def test_consensus_sums_to_one_synthetic() -> None:
    result = consensus_fair_prob(rows_of(tennis_event(FIVE_BOOKS)), "t1", "h2h")
    assert result.fair_probs is not None
    assert sum(result.fair_probs.values()) == pytest.approx(1.0, abs=1e-9)


def test_consensus_sums_to_one_on_real_fixture() -> None:
    raw = json.loads((FIXTURES / "soccer_epl_2026-11-09.json").read_text(encoding="utf-8"))
    rows = normalize_events([Event.model_validate(e) for e in raw]).rows
    checked = 0
    for event_id in {r.event_id for r in rows}:
        result = consensus_fair_prob(rows, event_id, "h2h")
        assert result.fair_probs is not None  # todos los eventos tienen >= 7 casas
        assert sum(result.fair_probs.values()) == pytest.approx(1.0, abs=1e-9)
        checked += 1
    assert checked == 20


def test_consensus_unknown_group_is_insufficient() -> None:
    result = consensus_fair_prob(rows_of(tennis_event(FIVE_BOOKS)), "otro", "h2h")
    assert result.n_books == 0 and result.fair_probs is None


# --- sharp_prob ---------------------------------------------------------------


def test_sharp_prob_prefers_pinnacle() -> None:
    books = {**FIVE_BOOKS, "pinnacle": (1.95, 1.95)}
    result = consensus_fair_prob(rows_of(tennis_event(books)), "t1", "h2h")
    assert result.sharp_book == "pinnacle"
    assert result.sharp_probs == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}


def test_sharp_prob_falls_back_to_lowest_margin_book() -> None:
    books = {**FIVE_BOOKS, "baja": (2.02, 1.98)}  # margen ~0.01, el menor
    result = consensus_fair_prob(rows_of(tennis_event(books)), "t1", "h2h", devig="proportional")
    assert result.sharp_book == "baja"
    assert result.sharp_probs is not None
    assert list(result.sharp_probs.values()) == pytest.approx(fair_probs([2.02, 1.98]))


def test_sharp_prob_available_even_with_few_books() -> None:
    result = consensus_fair_prob(rows_of(tennis_event({"pinnacle": (1.9, 1.9)})), "t1", "h2h")
    assert result.sufficient is False
    assert result.sharp_book == "pinnacle"


def test_ranked_rows_carry_sharp_prob() -> None:
    books = {**FIVE_BOOKS, "pinnacle": (1.95, 1.95)}
    ranked = rank(summarize_markets(rows_of(tennis_event(books))), now=NOW)
    assert all(r.sharp_book == "pinnacle" and r.sharp_prob == pytest.approx(0.5) for r in ranked)


# --- etiquetas y vista value --------------------------------------------------


@pytest.mark.parametrize(
    ("ev", "label"),
    [
        (0.05, "revisar"),
        (0.0201, "revisar"),
        (0.02, "neutro"),
        (0.0, "neutro"),
        (-0.0001, "evitar"),
        (None, "datos insuficientes"),
    ],
)
def test_label_ev(ev: float | None, label: str) -> None:
    assert label_ev(ev) == label


def test_value_view_sorted_by_ev_descending_and_skips_insufficient() -> None:
    events = [
        tennis_event(FIVE_BOOKS, event_id="t1"),
        tennis_event({"b1": (1.9, 1.9), "b2": (2.5, 1.5)}, event_id="pocas"),
    ]
    summaries = summarize_markets(rows_of(*events))
    ranked = rank(summaries, RankFilters(), now=NOW, view="value")
    assert [r.outcome for r in ranked] == ["A", "B"]
    evs = [r.ev for r in ranked]
    assert all(ev is not None for ev in evs)
    assert evs == sorted(evs, reverse=True)  # type: ignore[type-var]
    assert "pocas" not in {r.event_id for r in ranked}


def test_margin_view_still_includes_insufficient() -> None:
    events = [tennis_event({"b1": (1.9, 1.9), "b2": (2.5, 1.5)}, event_id="pocas")]
    ranked = rank(summarize_markets(rows_of(*events)), now=NOW)
    assert {r.event_id for r in ranked} == {"pocas"}


def test_invalid_view_rejected() -> None:
    with pytest.raises(ValueError):
        rank([], now=NOW, view="nope")  # type: ignore[arg-type]


# --- método power (default): corrige el sesgo hacia cuotas altas ---------------

# Mercado típico de fútbol con un favorito claro: la casa carga más margen al
# no favorito (cuota 7.50) que el proporcional no detecta.
SOCCER_ODDS = (1.40, 4.60, 7.50)


def test_default_devig_is_power() -> None:
    assert DEFAULT_DEVIG == "power"
    event = tennis_event(FIVE_BOOKS)
    default = consensus_fair_prob(rows_of(event), "t1", "h2h")
    power = consensus_fair_prob(rows_of(event), "t1", "h2h", devig="power")
    assert default.fair_probs == power.fair_probs


def test_power_consensus_known_values() -> None:
    result = consensus_fair_prob(rows_of(tennis_event(FIVE_BOOKS)), "t1", "h2h")
    outlier_a, outlier_b = fair_probs_power([2.20, 1.65])
    assert result.fair_probs is not None
    assert result.fair_probs["A"] == pytest.approx((4 * 0.5 + outlier_a) / 5)
    assert result.fair_probs["B"] == pytest.approx((4 * 0.5 + outlier_b) / 5)


def test_identical_books_ev_with_power() -> None:
    event = soccer_event({f"casa{i}": SOCCER_ODDS for i in range(5)})
    fair = fair_probs_power(list(SOCCER_ODDS))
    ranked = {r.outcome: r for r in rank(summarize_markets(rows_of(event)), now=NOW)}
    for (outcome, odds), prob in zip(
        zip(("L", "Draw", "V"), SOCCER_ODDS, strict=True), fair, strict=True
    ):
        assert ranked[outcome].ev == pytest.approx(odds * prob - 1)
        assert ranked[outcome].label == "evitar"


def test_power_removes_longshot_ev_inflation() -> None:
    """Con proporcional el no favorito sale con EV más alto que con power."""
    books = {f"casa{i}": SOCCER_ODDS for i in range(4)}
    books["blanda"] = (1.36, 4.80, 8.20)  # casa que paga más al no favorito
    event = soccer_event(books)
    power = {r.outcome: r for r in rank(summarize_markets(rows_of(event)), now=NOW)}
    prop = {
        r.outcome: r for r in rank(summarize_markets(rows_of(event), devig="proportional"), now=NOW)
    }
    longshot_power, longshot_prop = power["V"], prop["V"]
    assert longshot_power.best_price == longshot_prop.best_price == 8.20
    assert longshot_power.fair_prob is not None and longshot_prop.fair_prob is not None
    assert longshot_power.fair_prob < longshot_prop.fair_prob
    assert longshot_power.ev is not None and longshot_prop.ev is not None
    assert longshot_power.ev < longshot_prop.ev
    assert power["L"].fair_prob is not None and prop["L"].fair_prob is not None
    assert power["L"].fair_prob > prop["L"].fair_prob


def test_unknown_devig_method_raises() -> None:
    rows = rows_of(tennis_event(FIVE_BOOKS))
    with pytest.raises(ValueError, match="devig"):
        consensus_fair_prob(rows, "t1", "h2h", devig="x")  # type: ignore[arg-type]
