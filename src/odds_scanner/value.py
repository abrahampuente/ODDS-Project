"""Probabilidad justa por consenso de mercado y valor esperado (EV).

Funciones puras sobre filas normalizadas; sin I/O.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from odds_scanner.math import fair_probs, margin
from odds_scanner.normalize import OddsRow

MIN_BOOKS = 4
SHARP_BOOKS = ("pinnacle",)  # referencia preferida; si no está, la casa de menor margen
EV_REVIEW_THRESHOLD = 0.02

LABEL_REVIEW = "revisar"
LABEL_NEUTRAL = "neutro"
LABEL_AVOID = "evitar"
INSUFFICIENT_DATA = "datos insuficientes"

EV_DISCLAIMER = (
    "EV estimado con consenso de mercado. No garantiza ganancias; un EV "
    "positivo aislado suele ser una cuota desactualizada."
)


@dataclass(frozen=True)
class Consensus:
    """Probabilidades sin margen de un evento+mercado+línea."""

    n_books: int
    fair_probs: dict[str, float] | None  # None si hay menos de MIN_BOOKS casas
    sharp_book: str | None
    sharp_probs: dict[str, float] | None

    @property
    def sufficient(self) -> bool:
        """True si hay casas suficientes para estimar EV."""
        return self.fair_probs is not None


def _by_book(rows: Sequence[OddsRow]) -> dict[str, dict[str, float]]:
    """Cuotas por casa y resultado, conservando el orden de los resultados."""
    books: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        books[row.bookmaker][row.outcome] = row.price
    return books


def _fair_by_outcome(prices: dict[str, float]) -> dict[str, float]:
    return dict(zip(prices, fair_probs(list(prices.values())), strict=True))


def consensus_from_group(rows: Sequence[OddsRow]) -> Consensus:
    """Consenso para filas que ya pertenecen a un mismo evento+mercado+línea."""
    books = _by_book(rows)
    if not books:
        return Consensus(n_books=0, fair_probs=None, sharp_book=None, sharp_probs=None)

    sharp_book = next((b for b in SHARP_BOOKS if b in books), None)
    if sharp_book is None:
        sharp_book = min(books, key=lambda b: (margin(list(books[b].values())), b))
    sharp_probs = _fair_by_outcome(books[sharp_book])

    if len(books) < MIN_BOOKS:
        return Consensus(len(books), None, sharp_book, sharp_probs)

    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for prices in books.values():
        for outcome, prob in _fair_by_outcome(prices).items():
            totals[outcome] += prob
            counts[outcome] += 1
    averages = {o: totals[o] / counts[o] for o in totals}
    norm = sum(averages.values())  # 1.0 si todas las casas cubren los mismos resultados
    consensus = {o: p / norm for o, p in averages.items()}
    return Consensus(len(books), consensus, sharp_book, sharp_probs)


def consensus_fair_prob(
    rows: Sequence[OddsRow], event_id: str, market: str, line: float | None = None
) -> Consensus:
    """Quita el margen a cada casa y promedia por resultado (requiere MIN_BOOKS casas)."""
    group = [r for r in rows if (r.event_id, r.market, r.line) == (event_id, market, line)]
    return consensus_from_group(group)


def expected_value(best_price: float, fair_prob: float) -> float:
    """EV por unidad apostada: best_price * fair_prob - 1."""
    return best_price * fair_prob - 1.0


def label_ev(ev: float | None) -> str:
    """Etiqueta: > 2% revisar, 0-2% neutro, < 0 evitar; None = datos insuficientes."""
    if ev is None:
        return INSUFFICIENT_DATA
    if ev > EV_REVIEW_THRESHOLD:
        return LABEL_REVIEW
    if ev >= 0:
        return LABEL_NEUTRAL
    return LABEL_AVOID
