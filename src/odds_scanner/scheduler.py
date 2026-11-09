"""Programador de escaneos (alternativa a cron para correr como usuario no root).

Calcula las corridas en hora de Chile con zoneinfo, así los cambios de horario
(abril y septiembre) no corren la hora. Cada corrida: verifica cuota (gratis) y,
solo si la reserva alcanza, ejecuta scan -> rank -> export.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from odds_scanner.client import estimate_cost
from odds_scanner.normalize import CHILE_TZ

QUOTA_INSUFFICIENT = 2  # código de salida de `quota --need` cuando la reserva no alcanza

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


def run_once(job: ScanJob, *, runner: Runner, log: Log) -> int:
    """Ejecuta una corrida. Devuelve 0 si todo salió bien o el código del paso que falló."""
    commands = job_commands(job)
    log(f"inicio de corrida: {', '.join(job.sports)} (costo máximo {job.cost} consultas)")
    code = runner(commands[0])
    if code == QUOTA_INSUFFICIENT:
        log("reserva insuficiente o cuota desconocida: no se llama a la API en esta corrida")
        return code
    if code != 0:
        log(f"verificación de cuota falló (código {code}): no se llama a la API")
        return code
    for command in commands[1:]:
        code = runner(command)
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
) -> None:
    """Duerme hasta cada hora programada y ejecuta una corrida (max_runs para tests)."""
    runs = 0
    while max_runs is None or runs < max_runs:
        now = clock()
        target = next_run(now, times)
        log(f"próxima corrida: {target.astimezone(CHILE_TZ):%Y-%m-%d %H:%M %Z}")
        sleep(max(0.0, (target - now).total_seconds()))
        run_once(job, runner=runner, log=log)
        runs += 1
