"""Tests del módulo de matemática pura."""

import math as pymath
from itertools import combinations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from odds_scanner.math import (
    best_market_margin,
    break_even_prob,
    expected_value,
    fair_probs,
    fair_probs_power,
    implied_prob,
    margin,
    net_odds,
    overround,
    target_odds,
)

INVALID_ODDS = [1.0, 0.0, -2.5, 0.5, float("nan"), float("inf")]

# --- implied_prob / overround / margin ---------------------------------------


def test_implied_prob() -> None:
    assert implied_prob(2.0) == 0.5
    assert implied_prob(4.0) == 0.25


@pytest.mark.parametrize("odds", INVALID_ODDS)
def test_implied_prob_rejects_invalid_odds(odds: float) -> None:
    with pytest.raises(ValueError):
        implied_prob(odds)


def test_overround_three_way() -> None:
    assert overround([2.10, 3.40, 3.60]) == pytest.approx(1 / 2.10 + 1 / 3.40 + 1 / 3.60)


def test_margin_three_way() -> None:
    assert margin([2.10, 3.40, 3.60]) == pytest.approx(0.0484, abs=1e-3)


def test_margin_two_way() -> None:
    assert margin([1.91, 1.91]) == pytest.approx(0.0471, abs=1e-3)


def test_margin_fair_market_is_exactly_zero() -> None:
    assert margin([2.00, 2.00]) == 0.0


@pytest.mark.parametrize("func", [overround, margin, fair_probs])
def test_empty_list_raises(func: object) -> None:
    with pytest.raises(ValueError):
        func([])  # type: ignore[operator]


@pytest.mark.parametrize("func", [overround, margin, fair_probs])
@pytest.mark.parametrize("bad", INVALID_ODDS)
def test_list_with_invalid_odds_raises(func: object, bad: float) -> None:
    with pytest.raises(ValueError):
        func([2.0, bad])  # type: ignore[operator]


# --- fair_probs ---------------------------------------------------------------


def test_fair_probs_sum_to_one_and_keep_order() -> None:
    probs = fair_probs([2.10, 3.40, 3.60])
    assert len(probs) == 3
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    assert probs[0] > probs[1] > probs[2]  # menor cuota = mayor probabilidad
    ov = overround([2.10, 3.40, 3.60])
    assert probs[0] == pytest.approx((1 / 2.10) / ov)


# --- best_market_margin -------------------------------------------------------


def test_best_market_margin_uses_best_odds_per_outcome() -> None:
    # 3 resultados x 2 casas; las mejores son 2.10, 3.50, 3.60
    odds_by_outcome = [[2.05, 2.10], [3.40, 3.50], [3.60, 3.30]]
    assert best_market_margin(odds_by_outcome) == pytest.approx(margin([2.10, 3.50, 3.60]))


def test_best_market_margin_detects_arbitrage() -> None:
    # Casa A paga 2.20 al local, casa B paga 2.10 al visitante: 1/2.2 + 1/2.1 < 1
    odds_by_outcome = [[2.20, 1.80], [1.70, 2.10]]
    result = best_market_margin(odds_by_outcome)
    assert result < 0  # arbitraje
    assert result == pytest.approx(1 / 2.20 + 1 / 2.10 - 1)


def test_best_market_margin_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        best_market_margin([])
    with pytest.raises(ValueError):
        best_market_margin([[2.0], []])


def test_best_market_margin_rejects_invalid_odds() -> None:
    with pytest.raises(ValueError):
        best_market_margin([[2.0, 0.0], [2.0]])


# --- target_odds --------------------------------------------------------------


def test_target_odds_exact() -> None:
    assert target_odds(25_000, 500_000) == 20.0


def test_target_odds_partial_progress() -> None:
    assert target_odds(187_000, 500_000) == pytest.approx(2.674, abs=1e-3)


@pytest.mark.parametrize(
    ("stake", "target"),
    [(0, 500_000), (-1, 500_000), (25_000, 0), (25_000, 25_000), (30_000, 25_000)],
)
def test_target_odds_rejects_invalid(stake: int, target: int) -> None:
    with pytest.raises(ValueError):
        target_odds(stake, target)


# --- break_even_prob / expected_value ----------------------------------------


def test_break_even_prob() -> None:
    assert break_even_prob(2.5) == pytest.approx(0.4)


def test_break_even_prob_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        break_even_prob(1.0)


def test_expected_value() -> None:
    assert expected_value(2.10, 0.5) == pytest.approx(0.05)
    assert expected_value(2.00, 0.5) == pytest.approx(0.0)
    assert expected_value(1.80, 0.5) == pytest.approx(-0.10)


def test_expected_value_at_break_even_is_zero() -> None:
    assert expected_value(3.40, break_even_prob(3.40)) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("prob", [-0.1, 1.1, float("nan")])
def test_expected_value_rejects_invalid_prob(prob: float) -> None:
    with pytest.raises(ValueError):
        expected_value(2.0, prob)


def test_expected_value_rejects_invalid_odds() -> None:
    with pytest.raises(ValueError):
        expected_value(0.0, 0.5)


# --- propiedades --------------------------------------------------------------

odds_lists = st.lists(
    st.floats(min_value=1.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=20,
)


@given(odds_lists)
def test_property_fair_probs_sum_to_one(odds: list[float]) -> None:
    probs = fair_probs(odds)
    assert pymath.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert all(0.0 < p <= 1.0 for p in probs)


@given(odds_lists)
def test_property_margin_at_least_minus_one(odds: list[float]) -> None:
    assert margin(odds) >= -1.0


# --- fair_probs_power (corrige el sesgo favorito-longshot) --------------------


def test_power_without_margin_returns_implied() -> None:
    assert fair_probs_power([2.0, 2.0]) == pytest.approx([0.5, 0.5])
    assert fair_probs_power([1.5, 3.0]) == pytest.approx([2 / 3, 1 / 3])  # overround exacto 1


def test_power_sums_to_one_and_keeps_order() -> None:
    probs = fair_probs_power([2.10, 3.40, 3.60])
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    assert probs[0] > probs[1] > probs[2]


def test_power_uses_a_single_exponent() -> None:
    """p_i = implied_i ** k con el mismo k para todos los resultados."""
    odds = [1.30, 5.00, 9.50]
    probs = fair_probs_power(odds)
    exponents = [pymath.log(p) / pymath.log(1 / o) for p, o in zip(probs, odds, strict=True)]
    assert max(exponents) - min(exponents) < 1e-6
    assert exponents[0] > 1.0  # con margen positivo, k > 1


def test_power_gives_longshots_less_than_proportional() -> None:
    odds = [1.30, 5.00, 9.50]  # margen ~7,4%
    power, proportional = fair_probs_power(odds), fair_probs(odds)
    assert power[2] < proportional[2]  # el no favorito ya no queda inflado
    assert power[1] < proportional[1]
    assert power[0] > proportional[0]


def test_power_handles_negative_margin() -> None:
    probs = fair_probs_power([2.10, 2.10])  # overround < 1
    assert probs == pytest.approx([0.5, 0.5])


def test_power_single_outcome_and_invalid() -> None:
    assert fair_probs_power([1.5]) == [1.0]
    with pytest.raises(ValueError):
        fair_probs_power([])
    with pytest.raises(ValueError):
        fair_probs_power([2.0, 1.0])


@given(odds_lists)
def test_property_power_sums_to_one_and_keeps_order(odds: list[float]) -> None:
    probs = fair_probs_power(odds)
    assert pymath.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert len(probs) == len(odds)
    # Todos los pares (no solo consecutivos): a menor cuota, mayor o igual probabilidad.
    # El zip(strict=True) anterior comparaba listas de largo n y n-1 y fallaba siempre.
    for (oa, pa), (ob, pb) in combinations(zip(odds, probs, strict=True), 2):
        if oa < ob:
            assert pa >= pb - 1e-12
        elif ob < oa:
            assert pb >= pa - 1e-12


# --- net_odds (comisión de exchanges) -----------------------------------------


def test_net_odds_discounts_commission_on_profit() -> None:
    assert net_odds(3.0, 0.05) == pytest.approx(2.9)
    assert net_odds(5.0, 0.02) == pytest.approx(4.92)
    assert net_odds(2.0, 0.0) == 2.0


@pytest.mark.parametrize("commission", [-0.01, 1.0, 1.5])
def test_net_odds_rejects_invalid_commission(commission: float) -> None:
    with pytest.raises(ValueError):
        net_odds(2.0, commission)


def test_net_odds_rejects_invalid_odds() -> None:
    with pytest.raises(ValueError):
        net_odds(1.0, 0.05)
