"""Cliente HTTP de The Odds API v4 con control de cuota, caché y reintentos.

Reglas de seguridad: la API key nunca se escribe en disco, logs ni mensajes
de error. Toda llamada que consume cuota se valida contra la reserva mínima
ANTES de tocar la red.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

BASE_URL = "https://api.the-odds-api.com"
CACHE_TTL = timedelta(minutes=10)
MAX_RETRIES = 3
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))

logger = logging.getLogger(__name__)
# httpx registra la URL completa (con apiKey) a nivel INFO: se silencia.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# --- errores ------------------------------------------------------------------


class OddsApiError(RuntimeError):
    """Error al hablar con The Odds API (mensaje ya saneado, sin key)."""


class InvalidApiKeyError(OddsApiError):
    """La API rechazó la key (HTTP 401)."""


class QuotaGuardError(OddsApiError):
    """La llamada dejaría la cuota por debajo de la reserva mínima."""


# --- modelos ------------------------------------------------------------------


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Sport(_ApiModel):
    """Deporte de /v4/sports."""

    key: str
    group: str
    title: str
    description: str = ""
    active: bool
    has_outrights: bool = False


class Outcome(_ApiModel):
    """Resultado de un mercado con su cuota decimal."""

    name: str
    price: float
    point: float | None = None


class Market(_ApiModel):
    """Mercado (h2h, spreads, totals...) de una casa."""

    key: str
    last_update: datetime | None = None
    outcomes: list[Outcome]


class Bookmaker(_ApiModel):
    """Casa de apuestas con sus mercados."""

    key: str
    title: str
    last_update: datetime | None = None  # obsoleto en la API; usar Market.last_update
    markets: list[Market]


class Event(_ApiModel):
    """Evento con cuotas de /v4/sports/{sport}/odds."""

    id: str
    sport_key: str
    sport_title: str | None = None
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: list[Bookmaker] = []


@dataclass(frozen=True)
class QuotaStatus:
    """Último estado de cuota conocido (de los headers x-requests-*)."""

    remaining: int
    used: int
    last: int
    updated_at: str


# --- funciones puras ----------------------------------------------------------


def _clean(values: Sequence[str]) -> list[str]:
    """Normaliza una lista de claves: sin espacios, sin vacíos, sin duplicados."""
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def estimate_cost(regions: Sequence[str], markets: Sequence[str]) -> int:
    """Costo de /odds según la documentación: 1 por región por mercado.

    Es una cota superior: si la API no devuelve eventos, la llamada no cuesta.
    """
    regions_c, markets_c = _clean(regions), _clean(markets)
    if not regions_c or not markets_c:
        raise ValueError("se requiere al menos una región y un mercado")
    return len(regions_c) * len(markets_c)


def read_quota(data_dir: Path) -> QuotaStatus | None:
    """Lee data/quota.json; None si no existe o está corrupto."""
    try:
        raw = json.loads((data_dir / "quota.json").read_text(encoding="utf-8"))
        return QuotaStatus(
            remaining=int(raw["remaining"]),
            used=int(raw["used"]),
            last=int(raw["last"]),
            updated_at=str(raw["updated_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _header_int(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- cliente ------------------------------------------------------------------


class OddsClient:
    """Cliente de The Odds API v4. Usar como context manager o llamar a close()."""

    def __init__(
        self,
        api_key: str,
        *,
        data_dir: Path = Path("data"),
        fixtures_dir: Path = Path("fixtures"),
        min_quota_reserve: int = 50,
        cache_ttl: timedelta = CACHE_TTL,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = 1.0,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._api_key = api_key
        self.data_dir = data_dir
        self.cache_dir = data_dir / "cache"
        self.fixtures_dir = fixtures_dir
        self.min_quota_reserve = min_quota_reserve
        self.cache_ttl = cache_ttl
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=20.0)
        self._sleep = sleep
        self._clock = clock
        self.last_from_cache = False
        self.last_record_path: Path | None = None

    def __repr__(self) -> str:
        return f"OddsClient(data_dir={self.data_dir!s}, api_key=***)"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Cierra la conexión HTTP."""
        self._http.close()

    # --- API pública ---

    def quota(self) -> QuotaStatus | None:
        """Último estado de cuota guardado en disco."""
        return read_quota(self.data_dir)

    def estimate_cost(self, regions: Sequence[str], markets: Sequence[str]) -> int:
        """Ver `estimate_cost` del módulo."""
        return estimate_cost(regions, markets)

    def list_sports(self, *, include_all: bool = False) -> list[Sport]:
        """Deportes en temporada (o todos con include_all). No consume cuota."""
        params = {"all": "true"} if include_all else {}
        raw = self._get("/v4/sports", params, cost=0)
        return self._parse_list(Sport, raw)

    def get_odds(
        self,
        sport_key: str,
        regions: Sequence[str],
        markets: Sequence[str],
        *,
        record: bool = False,
    ) -> list[Event]:
        """Eventos con cuotas decimales. Valida la cuota antes de llamar."""
        cost = estimate_cost(regions, markets)
        params = {
            "regions": ",".join(_clean(regions)),
            "markets": ",".join(_clean(markets)),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        raw = self._get(f"/v4/sports/{sport_key}/odds", params, cost=cost)
        if record:
            self.last_record_path = self._record(sport_key, raw)
        return self._parse_list(Event, raw)

    # --- internos ---

    def _get(self, path: str, params: Mapping[str, str], *, cost: int) -> Any:
        cache_file = self._cache_file(path, params)
        cached = self._read_cache(cache_file)
        if cached is not None:
            self.last_from_cache = True
            logger.info("caché: %s %s", path, dict(params))
            return cached
        self.last_from_cache = False
        if cost > 0:
            self._guard(cost)
        logger.info("GET %s %s (costo estimado %d)", path, dict(params), cost)
        response = self._request(path, {**params, "apiKey": self._api_key})
        self._save_quota(response.headers)
        try:
            body = response.json()
        except ValueError as exc:
            raise OddsApiError(f"respuesta no es JSON válido en {path}") from exc
        self._write_cache(cache_file, path, params, body)
        return body

    def _guard(self, cost: int) -> None:
        quota = self.quota()
        if quota is None:
            raise QuotaGuardError(
                "cuota desconocida: ejecuta `odds-scanner sports` (gratis) para leerla "
                "antes de una llamada con costo"
            )
        after = quota.remaining - cost
        if after < self.min_quota_reserve:
            raise QuotaGuardError(
                f"llamada bloqueada: quedan {quota.remaining}, costo {cost}, "
                f"quedarían {after} < reserva {self.min_quota_reserve}"
            )

    def _request(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            last_try = attempt == attempts - 1
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                logger.warning("error de red en %s (%s)", path, type(exc).__name__)
                if last_try:
                    raise OddsApiError(
                        f"error de red en {path} tras {attempts} intentos ({type(exc).__name__})"
                    ) from None
                self._sleep(self._backoff(attempt, None))
                continue

            status = response.status_code
            if status == 401:
                self._save_quota(response.headers)
                raise InvalidApiKeyError(
                    "The Odds API rechazó la key (401). Revisa ODDS_API_KEY en .env "
                    "o tu plan en https://the-odds-api.com."
                )
            if status in RETRY_STATUSES:
                logger.warning("HTTP %d en %s (intento %d/%d)", status, path, attempt + 1, attempts)
                if last_try:
                    raise OddsApiError(f"HTTP {status} en {path} tras {attempts} intentos")
                self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue
            if status >= 400:
                self._save_quota(response.headers)
                raise OddsApiError(f"HTTP {status} en {path}: {self._error_message(response)}")
            return response
        raise AssertionError("inalcanzable")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return float(self.backoff_base * 2**attempt)

    def _error_message(self, response: httpx.Response) -> str:
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = response.text[:200]
        return self._scrub(message) or "sin detalle"

    def _scrub(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def _save_quota(self, headers: httpx.Headers) -> None:
        remaining = _header_int(headers, "x-requests-remaining")
        used = _header_int(headers, "x-requests-used")
        if remaining is None or used is None:
            return
        last = _header_int(headers, "x-requests-last") or 0
        data = {
            "remaining": remaining,
            "used": used,
            "last": last,
            "updated_at": self._clock().isoformat(),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "quota.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("cuota: restantes=%d usadas=%d última=%d", remaining, used, last)

    def _cache_file(self, path: str, params: Mapping[str, str]) -> Path:
        key = json.dumps({"path": path, "params": dict(sorted(params.items()))}, sort_keys=True)
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _read_cache(self, file: Path) -> Any:
        try:
            entry = json.loads(file.read_text(encoding="utf-8"))
            stored_at = datetime.fromisoformat(entry["stored_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self._clock() - stored_at > self.cache_ttl:
            return None
        return entry["body"]

    def _write_cache(self, file: Path, path: str, params: Mapping[str, str], body: Any) -> None:
        entry = {
            "stored_at": self._clock().isoformat(),
            "path": path,
            "params": dict(params),
            "body": body,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        file.write_text(self._scrub(json.dumps(entry)), encoding="utf-8")

    def _record(self, sport_key: str, body: Any) -> Path:
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        path = self.fixtures_dir / f"{sport_key}_{self._clock().date().isoformat()}.json"
        text = self._scrub(json.dumps(body, indent=2, ensure_ascii=False))
        path.write_text(text + "\n", encoding="utf-8")
        logger.info("respuesta grabada en %s", path)
        return path

    @staticmethod
    def _parse_list[T: BaseModel](model: type[T], raw: Any) -> list[T]:
        if not isinstance(raw, list):
            raise OddsApiError(f"se esperaba una lista JSON, llegó {type(raw).__name__}")
        try:
            return [model.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise OddsApiError(
                f"respuesta con formato inesperado ({exc.error_count()} errores)"
            ) from None
