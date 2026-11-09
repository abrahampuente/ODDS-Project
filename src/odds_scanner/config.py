"""Carga de configuración desde .env y variables de entorno.

Importar este módulo nunca falla por falta de key: la validación ocurre
solo cuando se pide la key con `Settings.require_api_key()`.
"""

import re
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from odds_scanner.exchanges import resolve_commissions

DEFAULT_ENV_FILE = Path(".env")
MIN_QUOTA_FLOOR = 50
_API_KEY_RE = re.compile(r"[0-9a-fA-F]{32}")


class ConfigError(RuntimeError):
    """Error de configuración con mensaje accionable para el usuario."""


class Settings(BaseSettings):
    """Configuración de la aplicación."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    odds_api_key: SecretStr | None = None
    # CLAUDE.md: con menos de 50 consultas se niega a llamar. El entorno puede subirla, no bajarla.
    min_quota_reserve: int = Field(default=MIN_QUOTA_FLOOR, ge=MIN_QUOTA_FLOOR)
    # Ajustes sobre las comisiones por defecto de exchanges: "betfair_ex_eu:0.05,matchbook:0.02"
    exchange_commissions: str = ""
    # Tope de consultas por mes calendario para el escaneo programado (scheduler)
    schedule_monthly_budget: int = Field(default=60, ge=0)

    def commissions(self) -> dict[str, float]:
        """Comisiones por casa (por defecto + ajustes); ConfigError si están mal escritas."""
        try:
            return resolve_commissions(self.exchange_commissions)
        except ValueError as exc:
            raise ConfigError(f"EXCHANGE_COMMISSIONS: {exc}") from None

    def require_api_key(self) -> str:
        """Devuelve la API key o lanza ConfigError si no está definida."""
        key = self.odds_api_key.get_secret_value().strip() if self.odds_api_key else ""
        if not key:
            raise ConfigError(
                "ODDS_API_KEY no está definida. Copia .env.example a .env "
                "y escribe tu key de https://the-odds-api.com."
            )
        return key


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Carga la configuración; las variables de entorno tienen prioridad sobre el .env."""
    return Settings(_env_file=env_file)


def is_valid_api_key(key: str) -> bool:
    """True si la key tiene el formato de The Odds API (32 caracteres hex)."""
    return _API_KEY_RE.fullmatch(key) is not None
