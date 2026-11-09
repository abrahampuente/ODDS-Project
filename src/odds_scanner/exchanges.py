"""Comisiones de los exchanges (Betfair, Matchbook, Smarkets).

Un exchange publica la cuota bruta, pero cobra comisión sobre la ganancia neta.
Para compararla con una casa tradicional hay que usar la cuota efectiva:
net = 1 + (bruta - 1) * (1 - comisión).

Los valores por defecto son la tarifa base estándar de cada exchange y pueden
cambiar según tu cuenta o país: ajústalos con EXCHANGE_COMMISSIONS en .env.
"""

from collections.abc import Mapping

DEFAULT_COMMISSIONS: Mapping[str, float] = {
    "betfair_ex_eu": 0.05,
    "betfair_ex_uk": 0.05,
    "betfair_ex_au": 0.05,
    "matchbook": 0.02,
    "smarkets": 0.02,
}


def parse_commissions(spec: str) -> dict[str, float]:
    """'betfair_ex_eu:0.04, matchbook:0.015' -> {'betfair_ex_eu': 0.04, 'matchbook': 0.015}.

    Acepta porcentaje o fracción ('4%' o '0.04'). Cadena vacía -> {}.
    """
    result: dict[str, float] = {}
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        key, sep, value = part.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise ValueError(f"comisión inválida: {part!r} (formato casa:0.05)")
        try:
            rate = float(value[:-1]) / 100 if value.endswith("%") else float(value)
        except ValueError:
            raise ValueError(f"comisión inválida: {part!r} (formato casa:0.05)") from None
        if not 0.0 <= rate < 1.0:
            raise ValueError(f"comisión fuera de rango en {part!r} (debe estar en [0, 1))")
        result[key] = rate
    return result


def resolve_commissions(overrides: str = "") -> dict[str, float]:
    """Comisiones por defecto con los ajustes del usuario encima."""
    return {**DEFAULT_COMMISSIONS, **parse_commissions(overrides)}
