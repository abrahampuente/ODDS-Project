"""Normaliza eventos de la API en filas planas (una por casa/mercado/línea/resultado).

Descarta y cuenta: precios <= 1.0, mercados lay de exchanges y mercados
incompletos (p. ej. h2h de fútbol sin los 3 resultados).
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from odds_scanner.client import Event, Outcome

CHILE_TZ = ZoneInfo("America/Santiago")
DRAW = "Draw"

# Motivos de descarte (claves del resumen).
INVALID_PRICE = "invalid_price"
INCOMPLETE_MARKET = "incomplete_market"
LAY_MARKET = "lay_market"


class OddsRow(BaseModel):
    """Una cuota de una casa para un resultado de un mercado."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    sport: str
    commence_time_utc: datetime
    commence_time_cl: datetime
    home: str
    away: str
    bookmaker: str
    market: str
    line: float | None  # clave de agrupación: point (totals) o point del local (spreads)
    outcome: str
    point: float | None
    price: float
    last_update: datetime | None


@dataclass
class NormalizeResult:
    """Filas válidas más el conteo de filas descartadas por motivo."""

    rows: list[OddsRow] = field(default_factory=list)
    discarded: Counter[str] = field(default_factory=Counter)

    @property
    def discarded_total(self) -> int:
        """Total de filas descartadas."""
        return sum(self.discarded.values())


def to_chile(moment: datetime) -> datetime:
    """Convierte un datetime con zona horaria a hora de Chile continental."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("se requiere un datetime con zona horaria")
    return moment.astimezone(CHILE_TZ)


def _line(market_key: str, outcome: Outcome, home: str) -> float | None:
    """Línea del mercado: totals usa point; spreads usa el point visto desde el local."""
    if outcome.point is None:
        return None
    if market_key.startswith("spreads"):
        return outcome.point if outcome.name == home else -outcome.point
    return outcome.point


def _is_complete(market_key: str, names: set[str], event: Event) -> bool:
    """Valida que un grupo (casa+mercado+línea) tenga todos sus resultados."""
    if market_key == "h2h":
        required = {event.home_team, event.away_team}
        if event.sport_key.startswith("soccer"):
            required.add(DRAW)
        return required <= names
    if market_key == "totals":
        return {"Over", "Under"} <= names
    if market_key == "spreads":
        return {event.home_team, event.away_team} <= names
    return len(names) >= 2


def normalize_events(events: Iterable[Event]) -> NormalizeResult:
    """Aplana eventos en filas OddsRow, descartando datos inválidos o incompletos."""
    result = NormalizeResult()
    for event in events:
        start_cl = to_chile(event.commence_time)
        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                if market.key.endswith("_lay"):
                    result.discarded[LAY_MARKET] += len(market.outcomes)
                    continue
                groups: dict[float | None, list[Outcome]] = defaultdict(list)
                for outcome in market.outcomes:
                    if outcome.price <= 1.0:
                        result.discarded[INVALID_PRICE] += 1
                        continue
                    groups[_line(market.key, outcome, event.home_team)].append(outcome)
                updated = market.last_update or bookmaker.last_update
                for line, outs in groups.items():
                    if not _is_complete(market.key, {o.name for o in outs}, event):
                        result.discarded[INCOMPLETE_MARKET] += len(outs)
                        continue
                    result.rows.extend(
                        OddsRow(
                            event_id=event.id,
                            sport=event.sport_key,
                            commence_time_utc=event.commence_time,
                            commence_time_cl=start_cl,
                            home=event.home_team,
                            away=event.away_team,
                            bookmaker=bookmaker.key,
                            market=market.key,
                            line=line,
                            outcome=o.name,
                            point=o.point,
                            price=o.price,
                            last_update=updated,
                        )
                        for o in outs
                    )
    return result
