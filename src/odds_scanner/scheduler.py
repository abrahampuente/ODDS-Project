"""Programador de escaneos (alternativa a cron para correr como usuario no root).

Calcula las corridas en hora de Chile con zoneinfo, así los cambios de horario
(abril y septiembre) no corren la hora. Cada corrida: verifica cuota (gratis) y,
solo si la reserva alcanza, ejecuta scan -> rank -> export.

Además lleva un tope mensual (MonthlyBudget): cada scan (aun si falla) suma su costo
máximo al mes calendario en curso (hora de Chile) y, si la siguiente corrida
pasaría el tope, se salta sin llamar a la API.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from odds_scanner.client import estimate_cost
from odds_scanner.normalize import CHILE_TZ

QUOTA_INSUFFICIENT = 2  # código de salida de `quota --need` cuando la reserva no alcanza
BUDGET_EXHAUSTED = 3  # la corrida pasaría el tope mensual del programador

Runner = Callable[[list[str]], int]
Log = Callable[[str], None]


@dataclass(frozen=True)
class ScanJob:
    """Qué escanear en cada corrida."""

    sports: list[str]
    markets: list[str]
    regions: list[str]

    @property
    def cost(self) -> int:
        """Consultas máximas que gasta una corrida."""
        return estimate_cost(self.regions, self.markets) * len(self.sports)


def month_key(moment: datetime, tz: ZoneInfo = CHILE_TZ) -> str:
    """Mes calendario en hora de Chile, p. ej. '2026-11'."""
    return moment.astimezone(tz).strftime("%Y-%m")


@dataclass(frozen=True)
class MonthlyBudget:
    """Tope de consultas por mes para el programador, persistido en un JSON."""

    path: Path
    limit: int

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("el tope mensual debe ser >= 0")

    def spent(self, month: str) -> int:
        """Consultas usadas en `month`; 0 si no hay registro, es de otro mes o está dañado."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict) or data.get("month") != month:
            return 0
        spent = data.get("spent")
        return spent if isinstance(spent, int) and spent >= 0 else 0

    def allows(self, month: str, cost: int) -> bool:
        """True si gastar `cost` más en `month` no pasa el tope."""
        return self.spent(month) + cost <= self.limit

    def add(self, month: str, amount: int) -> int:
        """Suma `amount` al mes y devuelve el total gastado."""
        total = self.spent(month) + amount
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"month": month, "spent": total}), encoding="utf-8")
        return total


def parse_times(spec: str) -> list[time]:
    """'10:00,18:00' -> [time(10, 0), time(18, 0)] (ordenadas, sin duplicados)."""
    result = set()
    for part in spec.split(","):
        part = part.strip()
        try:
            hours, minutes = part.split(":")
            result.add(time(int(hours), int(minutes)))
        except ValueError:
            raise ValueError(f"hora inválida: {part!r} (formato HH:MM)") from None
    if not result:
        raise ValueError("se requiere al menos una hora")
    return sorted(result)


def next_run(now: datetime, times: Sequence[time], tz: ZoneInfo = CHILE_TZ) -> datetime:
    """Próxima corrida estrictamente posterior a `now`, en UTC-aware."""
    local_now = now.astimezone(tz)
    for day_offset in range(3):
        day = local_now.date() + timedelta(days=day_offset)
        for at in sorted(times):
            candidate = datetime.combine(day, at, tzinfo=tz)
            if candidate > local_now:
                return candidate.astimezone(now.tzinfo)
    raise ValueError("no se pudo calcular la próxima corrida")  # pragma: no cover


def job_commands(job: ScanJob) -> list[list[str]]:
    """Subcomandos de una corrida: verificar cuota, scan, rank, export."""
    return [
        ["quota", "--refresh", "--need", str(job.cost)],
        [
            "scan",
            "--sports",
            ",".join(job.sports),
            "--markets",
            ",".join(job.markets),
            "--regions",
            ",".join(job.regions),
            "--yes",
        ],
        ["rank"],
        ["export", "--format", "csv"],
    ]


def fixture_commands(fixture: Path) -> list[list[str]]:
    """Subcomandos de una corrida offline: scan desde la fixture, rank, export (sin API)."""
    return [
        ["scan", "--from-fixture", str(fixture), "--yes"],
        ["rank"],
        ["export", "--format", "csv"],
    ]


def run_from_fixture(fixture: Path, *, runner: Runner, log: Log) -> int:
    """Corrida completa sin API (no verifica cuota ni toca el tope mensual)."""
    log(f"inicio de corrida offline: fixture {fixture} (0 consultas a la API)")
    for command in fixture_commands(fixture):
        code = runner(command)
        if code != 0:
            log(f"paso `{command[0]}` falló (código {code}); se detiene la corrida")
            return code
    log("corrida completa")
    return 0


def run_once(
    job: ScanJob,
    *,
    runner: Runner,
    log: Log,
    budget: MonthlyBudget | None = None,
    now: datetime | None = None,
) -> int:
    """Ejecuta una corrida. Devuelve 0 si todo salió bien o el código del paso que falló.

    Con `budget` (requiere `now`), no llama a la API si se pasaría el tope mensual.
    """
    commands = job_commands(job)
    log(f"inicio de corrida: {', '.join(job.sports)} (costo máximo {job.cost} consultas)")
    month: str | None = None
    if budget is not None:
        if now is None:
            raise ValueError("run_once con budget requiere now")
        month = month_key(now)
        if not budget.allows(month, job.cost):
            log(
                f"tope mensual alcanzado: {budget.spent(month)}/{budget.limit} consultas en "
                f"{month}; esta corrida ({job.cost}) lo pasaría. No se llama a la API."
            )
            return BUDGET_EXHAUSTED
    code = runner(commands[0])
    if code == QUOTA_INSUFFICIENT:
        log("reserva insuficiente o cuota desconocida: no se llama a la API en esta corrida")
        return code
    if code != 0:
        log(f"verificación de cuota falló (código {code}): no se llama a la API")
        return code
    for command in commands[1:]:
        code = runner(command)
        if command[0] == "scan" and budget is not None and month is not None:
            # Se descuenta aunque el scan falle a medias: puede haber gastado cuota.
            total = budget.add(month, job.cost)
            log(f"tope mensual: {total}/{budget.limit} consultas usadas en {month}")
        if code != 0:
            log(f"paso `{command[0]}` falló (código {code}); se detiene la corrida")
            return code
    log("corrida completa")
    return 0


def run_forever(
    job: ScanJob,
    times: Sequence[time],
    *,
    runner: Runner,
    log: Log,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
    max_runs: int | None = None,
    budget: MonthlyBudget | None = None,
) -> None:
    """Duerme hasta cada hora programada y ejecuta una corrida (max_runs para tests)."""
    runs = 0
    while max_runs is None or runs < max_runs:
        now = clock()
        target = next_run(now, times)
        log(f"próxima corrida: {target.astimezone(CHILE_TZ):%Y-%m-%d %H:%M %Z}")
        sleep(max(0.0, (target - now).total_seconds()))
        run_once(job, runner=runner, log=log, budget=budget, now=clock())
        runs += 1
