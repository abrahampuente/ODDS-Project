"""Matemática pura de cuotas decimales. Sin I/O ni estado.

Convenciones: cuotas en formato decimal (> 1.0), probabilidades en [0, 1],
dinero en enteros (CLP).
"""

import math
from collections.abc import Sequence


def _validate_odds(odds: float) -> None:
    """Lanza ValueError si la cuota no es un decimal finito > 1.0."""
    if not math.isfinite(odds) or odds <= 1.0:
        raise ValueError(f"cuota inválida: {odds!r} (debe ser finita y > 1.0)")


def _validate_list(odds_list: Sequence[float]) -> None:
    """Lanza ValueError si la lista está vacía o contiene cuotas inválidas."""
    if not odds_list:
        raise ValueError("la lista de cuotas está vacía")
    for odds in odds_list:
        _validate_odds(odds)


def implied_prob(odds: float) -> float:
    """Probabilidad implícita de una cuota: 1 / odds."""
    _validate_odds(odds)
    return 1.0 / odds


def overround(odds_list: Sequence[float]) -> float:
    """Suma de probabilidades implícitas de un mercado (1.0 = mercado justo)."""
    _validate_list(odds_list)
    return math.fsum(1.0 / odds for odds in odds_list)


def margin(odds_list: Sequence[float]) -> float:
    """Margen de la casa: overround - 1 (0.048 = 4,8%)."""
    return overround(odds_list) - 1.0


def fair_probs(odds_list: Sequence[float]) -> list[float]:
    """Probabilidades sin margen por método proporcional; suman 1 y conservan el orden."""
    total = overround(odds_list)
    return [(1.0 / odds) / total for odds in odds_list]


def fair_probs_power(odds_list: Sequence[float], *, tol: float = 1e-12) -> list[float]:
    """Probabilidades sin margen por método power: p_i = (1/odds_i) ** k, con k tal que sumen 1.

    A diferencia del proporcional, carga más margen a los resultados de cuota alta
    (sesgo favorito-longshot), así no infla la probabilidad de los no favoritos.
    Con un mercado sin margen devuelve las probabilidades implícitas tal cual.
    """
    _validate_list(odds_list)
    implied = [1.0 / odds for odds in odds_list]
    if len(implied) == 1:
        return [1.0]

    def excess(k: float) -> float:
        return math.fsum(p**k for p in implied) - 1.0

    # excess(k) decrece con k: en k -> 0 vale n - 1 > 0 y tiende a -1 cuando k crece.
    low, high = 1e-9, 1.0
    while excess(high) > 0:
        high *= 2.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if excess(mid) > 0:
            low = mid
        else:
            high = mid
        if high - low < tol:
            break
    k = (low + high) / 2.0
    probs = [p**k for p in implied]
    total = math.fsum(probs)
    return [p / total for p in probs]


def net_odds(odds: float, commission: float) -> float:
    """Cuota efectiva de un exchange tras cobrar comisión sobre la ganancia neta.

    net = 1 + (odds - 1) * (1 - commission). commission en [0, 1).
    """
    _validate_odds(odds)
    if not 0.0 <= commission < 1.0:
        raise ValueError(f"comisión inválida: {commission!r} (debe estar en [0, 1))")
    return 1.0 + (odds - 1.0) * (1.0 - commission)


def best_market_margin(odds_by_outcome: Sequence[Sequence[float]]) -> float:
    """Margen usando la mejor cuota de cada resultado entre casas.

    `odds_by_outcome[i]` son las cuotas de todas las casas para el resultado i.
    Un valor negativo indica arbitraje.
    """
    if not odds_by_outcome or any(not offers for offers in odds_by_outcome):
        raise ValueError("cada resultado necesita al menos una cuota")
    for offers in odds_by_outcome:
        _validate_list(offers)
    return margin([max(offers) for offers in odds_by_outcome])


def target_odds(stake: int, target: int) -> float:
    """Cuota necesaria para convertir `stake` en `target` (CLP) en una sola apuesta."""
    if stake <= 0:
        raise ValueError(f"stake debe ser > 0, recibido {stake}")
    if target <= stake:
        raise ValueError(f"target ({target}) debe ser mayor que stake ({stake})")
    return target / stake


def break_even_prob(odds: float) -> float:
    """Probabilidad mínima de acierto para no perder a largo plazo (= 1 / odds)."""
    return implied_prob(odds)


def expected_value(odds: float, prob: float) -> float:
    """Valor esperado por unidad apostada: odds * prob - 1."""
    _validate_odds(odds)
    if not 0.0 <= prob <= 1.0:
        raise ValueError(f"probabilidad inválida: {prob!r} (debe estar en [0, 1])")
    return odds * prob - 1.0
