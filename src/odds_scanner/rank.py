"""Resumen por evento+mercado+línea y ranking por margen combinado o por EV.

Funciones puras: reciben filas normalizadas y devuelven modelos; sin I/O.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from odds_scanner.math import margin
from odds_scanner.normalize import OddsRow
from odds_scanner.value import (
    DEFAULT_DEVIG,
    DevigMethod,
    consensus_from_group,
    expected_value,
    label_ev,
)

View = Literal["margin", "value"]

ARBITRAGE_WARNING = (
    "Margen combinado negativo (posible arbitraje): casi siempre es una cuota "
    "desactualizada o una comisión de exchange mal configurada. Verifica antes de confiar."
)


class BestPrice(BaseModel):
    """Mejor cuota de un resultado y la casa que la ofrece."""

    model_config = ConfigDict(frozen=True)

    outcome: str
    point: float | None
    price: float  # cuota efectiva (neta de comisión en exchanges)
    bookmaker: str
    gross_price: float  # cuota publicada; difiere de price solo en exchanges
    commission: float = 0.0


class MarketSummary(BaseModel):
    """Análisis de un evento+mercado+línea entre todas las casas."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    sport: str
    home: str
    away: str
    commence_time_utc: datetime
    commence_time_cl: datetime
    market: str
    line: float | None
    margin_by_book: dict[str, float]
    best_prices: list[BestPrice]
    best_market_margin: float
    is_arbitrage: bool
    fair_probs: dict[str, float] | None  # consenso sin margen; None si faltan casas
    sharp_book: str | None
    sharp_probs: dict[str, float] | None

    @property
    def n_books(self) -> int:
        """Cantidad de casas con el mercado completo."""
        return len(self.margin_by_book)

    def best(self, outcome: str) -> BestPrice:
        """Mejor cuota de un resultado; KeyError si no existe."""
        for best in self.best_prices:
            if best.outcome == outcome:
                return best
        raise KeyError(outcome)


class RankedOutcome(BaseModel):
    """Fila del ranking: un resultado con su mejor cuota y los márgenes."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    sport: str
    home: str
    away: str
    commence_time_utc: datetime
    commence_time_cl: datetime
    market: str
    line: float | None
    outcome: str
    point: float | None
    best_price: float  # cuota efectiva (neta de comisión en exchanges)
    bookmaker: str
    gross_price: float  # cuota que verás en la casa
    commission: float
    book_margin: float
    best_market_margin: float
    is_arbitrage: bool
    n_books: int
    fair_prob: float | None
    ev: float | None
    label: str
    sharp_book: str | None
    sharp_prob: float | None


@dataclass(frozen=True)
class RankFilters:
    """Filtros del ranking. None = sin filtro."""

    sports: frozenset[str] | set[str] | None = None
    markets: frozenset[str] | set[str] | None = None
    within_hours: float | None = None
    min_odds: float | None = None
    max_odds: float | None = None

    def __post_init__(self) -> None:
        if self.within_hours is not None and self.within_hours < 0:
            raise ValueError("within_hours debe ser >= 0")
        if (
            self.min_odds is not None
            and self.max_odds is not None
            and self.min_odds > self.max_odds
        ):
            raise ValueError("min_odds no puede ser mayor que max_odds")


def summarize_markets(
    rows: Iterable[OddsRow], devig: DevigMethod = DEFAULT_DEVIG
) -> list[MarketSummary]:
    """Agrupa filas por evento+mercado+línea y calcula márgenes y mejores cuotas.

    `devig` es el método para quitar el margen al estimar la probabilidad justa.
    """
    groups: dict[tuple[str, str, float | None], list[OddsRow]] = defaultdict(list)
    for row in rows:
        groups[(row.event_id, row.market, row.line)].append(row)

    summaries = []
    for group in groups.values():
        by_book: dict[str, list[OddsRow]] = defaultdict(list)
        for row in group:
            by_book[row.bookmaker].append(row)
        margin_by_book = {
            book: margin([r.price for r in book_rows]) for book, book_rows in by_book.items()
        }
        best: dict[str, OddsRow] = {}
        for row in group:  # el orden de inserción conserva el orden de los resultados
            current = best.get(row.outcome)
            if current is None or (row.price, current.bookmaker) > (current.price, row.bookmaker):
                best[row.outcome] = row
        best_prices = [
            BestPrice(
                outcome=o,
                point=r.point,
                price=r.price,
                bookmaker=r.bookmaker,
                gross_price=r.gross_price,
                commission=r.commission,
            )
            for o, r in best.items()
        ]
        combined = margin([b.price for b in best_prices])
        consensus = consensus_from_group(group, devig)
        first = group[0]
        summaries.append(
            MarketSummary(
                event_id=first.event_id,
                sport=first.sport,
                home=first.home,
                away=first.away,
                commence_time_utc=first.commence_time_utc,
                commence_time_cl=first.commence_time_cl,
                market=first.market,
                line=first.line,
                margin_by_book=margin_by_book,
                best_prices=best_prices,
                best_market_margin=combined,
                is_arbitrage=combined < 0,
                fair_probs=consensus.fair_probs,
                sharp_book=consensus.sharp_book,
                sharp_probs=consensus.sharp_probs,
            )
        )
    return summaries


def _passes_group_filters(s: MarketSummary, f: RankFilters, now: datetime) -> bool:
    if f.sports is not None and s.sport not in f.sports:
        return False
    if f.markets is not None and s.market not in f.markets:
        return False
    if f.within_hours is not None:
        hours = (s.commence_time_utc - now).total_seconds() / 3600
        if not 0 <= hours <= f.within_hours:
            return False
    return True


def _passes_odds_filter(price: float, f: RankFilters) -> bool:
    if f.min_odds is not None and price < f.min_odds:
        return False
    return not (f.max_odds is not None and price > f.max_odds)


def rank(
    summaries: Sequence[MarketSummary],
    filters: RankFilters | None = None,
    *,
    now: datetime,
    view: View = "margin",
) -> list[RankedOutcome]:
    """Ranking de resultados.

    view="margin": margen combinado ascendente (mejor primero).
    view="value": EV descendente; omite resultados sin EV (datos insuficientes).
    """
    if view not in ("margin", "value"):
        raise ValueError(f"vista desconocida: {view!r}")
    f = filters or RankFilters()
    ranked: list[tuple[tuple[float, float, datetime, str, str, float, int], RankedOutcome]] = []
    for s in summaries:
        if not _passes_group_filters(s, f, now):
            continue
        for index, best in enumerate(s.best_prices):
            if not _passes_odds_filter(best.price, f):
                continue
            fair = s.fair_probs[best.outcome] if s.fair_probs is not None else None
            ev = expected_value(best.price, fair) if fair is not None else None
            if view == "value" and ev is None:
                continue
            primary = -ev if view == "value" and ev is not None else s.best_market_margin
            secondary = s.best_market_margin if view == "value" else 0.0
            key = (
                primary,
                secondary,
                s.commence_time_utc,
                s.event_id,
                s.market,
                s.line if s.line is not None else 0.0,
                index,
            )
            sharp = s.sharp_probs.get(best.outcome) if s.sharp_probs is not None else None
            ranked.append(
                (
                    key,
                    RankedOutcome(
                        event_id=s.event_id,
                        sport=s.sport,
                        home=s.home,
                        away=s.away,
                        commence_time_utc=s.commence_time_utc,
                        commence_time_cl=s.commence_time_cl,
                        market=s.market,
                        line=s.line,
                        outcome=best.outcome,
                        point=best.point,
                        best_price=best.price,
                        bookmaker=best.bookmaker,
                        gross_price=best.gross_price,
                        commission=best.commission,
                        book_margin=s.margin_by_book[best.bookmaker],
                        best_market_margin=s.best_market_margin,
                        is_arbitrage=s.is_arbitrage,
                        n_books=s.n_books,
                        fair_prob=fair,
                        ev=ev,
                        label=label_ev(ev),
                        sharp_book=s.sharp_book,
                        sharp_prob=sharp,
                    ),
                )
            )
    ranked.sort(key=lambda item: item[0])
    return [row for _, row in ranked]
