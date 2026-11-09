"""Reloj de la aplicación (aislado para poder fijarlo en tests)."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Fecha y hora actual en UTC."""
    return datetime.now(UTC)
