"""Tests de fase 8: doctor con key en el entorno, comando quota, pipeline y programador."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
import respx
from typer.testing import CliRunner

from odds_scanner.cli import app
from odds_scanner.client import BASE_URL
from odds_scanner.scheduler import (
    BUDGET_EXHAUSTED,
    MonthlyBudget,
    ScanJob,
    fixture_commands,
    job_commands,
    month_key,
    next_run,
    parse_times,
    run_forever,
    run_from_fixture,
    run_once,
)
from tests.conftest import FAKE_KEY

runner = CliRunner()
SPORTS_URL = f"{BASE_URL}/v4/sports"
QUOTA_HEADERS = {"x-requests-remaining": "120", "x-requests-used": "380", "x-requests-last": "0"}


def seed_quota(workdir: Path, remaining: int) -> None:
    (workdir / "data" / "quota.json").write_text(
        json.dumps({"remaining": remaining, "used": 0, "last": 0, "updated_at": "x"}),
        encoding="utf-8",
    )


# --- doctor en contenedor -----------------------------------------------------


def test_doctor_accepts_key_from_environment(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", FAKE_KEY)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ODDS_API_KEY del entorno" in result.output
    assert FAKE_KEY not in result.output


# --- quota --------------------------------------------------------------------


def test_quota_shows_saved_status(workdir: Path) -> None:
    seed_quota(workdir, 300)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "restantes=300" in result.output


def test_quota_unknown_without_refresh_exits_2(workdir: Path) -> None:
    result = runner.invoke(app, ["quota", "--need", "1"])
    assert result.exit_code == 2
    assert "desconocida" in result.output


def test_quota_refresh_uses_free_endpoint(workdir: Path) -> None:
    (workdir / ".env").write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    with respx.mock:
        route = respx.get(SPORTS_URL).respond(200, json=[], headers=QUOTA_HEADERS)
        result = runner.invoke(app, ["quota", "--refresh", "--need", "4"])
    assert result.exit_code == 0, result.output
    assert route.called
    assert "restantes=120" in result.output and "alcanza" in result.output


@pytest.mark.parametrize("command", [["quota"], ["fetch", "soccer_epl"], ["scan", "--sports", "x"]])
def test_invalid_config_fails_cleanly_without_leaking_key(
    workdir: Path, command: list[str]
) -> None:
    """Auditoría fase 9: MIN_QUOTA_RESERVE < 50 se rechaza con mensaje claro, sin traceback."""
    (workdir / ".env").write_text(
        f"ODDS_API_KEY={FAKE_KEY}\nMIN_QUOTA_RESERVE=0\n", encoding="utf-8"
    )
    seed_quota(workdir, 500)
    result = runner.invoke(app, command)
    assert result.exit_code == 1
    assert "MIN_QUOTA_RESERVE" in result.output and ">= 50" in result.output
    assert "Traceback" not in result.output
    assert FAKE_KEY not in result.output
    assert not isinstance(result.exception, ValueError)  # salida controlada (typer.Exit)


def test_non_numeric_config_value_fails_cleanly(workdir: Path) -> None:
    (workdir / ".env").write_text("SCHEDULE_MONTHLY_BUDGET=muchas\n", encoding="utf-8")
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 1
    assert "SCHEDULE_MONTHLY_BUDGET debe ser un número" in result.output
    assert "muchas" not in result.output  # nunca se repite el valor ingresado


def test_cli_apps_never_show_locals_in_tracebacks() -> None:
    from odds_scanner.ledger_cli import ledger_app

    assert app.pretty_exceptions_show_locals is False
    assert ledger_app.pretty_exceptions_show_locals is False


def test_quota_need_above_reserve_exits_2(workdir: Path) -> None:
    seed_quota(workdir, 53)
    result = runner.invoke(app, ["quota", "--need", "4"])  # 53 - 4 = 49 < 50
    assert result.exit_code == 2
    assert "reserva insuficiente" in result.output


# --- pipeline (servicio scanner de docker compose) ----------------------------


def test_pipeline_runs_commands_in_order(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("odds_scanner.cli._run_subcommand", lambda cmd: calls.append(cmd) or 0)
    result = runner.invoke(
        app, ["pipeline", "--sports", "soccer_epl", "--markets", "h2h", "--regions", "eu"]
    )
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["quota", "scan", "rank", "export"]
    assert calls[0] == ["quota", "--refresh", "--need", "1"]
    assert calls[1][-1] == "--yes"


def test_pipeline_stops_when_quota_insufficient(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 2 if cmd[0] == "quota" else 0

    monkeypatch.setattr("odds_scanner.cli._run_subcommand", fake)
    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 2
    assert [c[0] for c in calls] == ["quota"]
    assert "no se llama a la API" in result.output


# --- programador --------------------------------------------------------------


def test_parse_times() -> None:
    assert parse_times("10:00, 18:00") == [time(10, 0), time(18, 0)]
    assert parse_times("18:00,10:00,10:00") == [time(10, 0), time(18, 0)]
    for bad in ("", "25:00", "10", "aa:bb"):
        with pytest.raises(ValueError):
            parse_times(bad)


TIMES = [time(10, 0), time(18, 0)]


@pytest.mark.parametrize(
    ("now_utc", "expected_utc"),
    [
        ("2026-11-09T12:00", "2026-11-09T13:00"),  # 09:00 CLST -> 10:00 CLST
        ("2026-11-09T13:00", "2026-11-09T21:00"),  # justo a las 10:00 -> la siguiente es 18:00
        ("2026-11-09T22:00", "2026-11-10T13:00"),  # 19:00 -> mañana 10:00
        ("2026-07-01T12:00", "2026-07-01T14:00"),  # invierno CLT (UTC-4)
        ("2026-04-04T22:00", "2026-04-05T14:00"),  # cruza el cambio de abril (-3 -> -4)
        ("2026-09-05T23:00", "2026-09-06T13:00"),  # cruza el cambio de septiembre (-4 -> -3)
    ],
)
def test_next_run_in_chile_time(now_utc: str, expected_utc: str) -> None:
    now = datetime.fromisoformat(now_utc).replace(tzinfo=UTC)
    expected = datetime.fromisoformat(expected_utc).replace(tzinfo=UTC)
    assert next_run(now, TIMES) == expected


JOB = ScanJob(sports=["soccer_epl", "soccer_spain_la_liga"], markets=["h2h"], regions=["eu"])


def test_job_commands() -> None:
    commands = job_commands(JOB)
    assert commands[0] == ["quota", "--refresh", "--need", "2"]
    assert commands[1] == [
        "scan",
        "--sports",
        "soccer_epl,soccer_spain_la_liga",
        "--markets",
        "h2h",
        "--regions",
        "eu",
        "--yes",
    ]
    assert commands[2:] == [["rank"], ["export", "--format", "csv"]]


def test_run_once_skips_when_quota_insufficient() -> None:
    calls: list[list[str]] = []
    logs: list[str] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 2

    assert run_once(JOB, runner=fake, log=logs.append) == 2
    assert len(calls) == 1
    assert any("reserva insuficiente" in line and "no se llama" in line for line in logs)


def test_run_once_stops_on_failed_step() -> None:
    calls: list[list[str]] = []
    logs: list[str] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 1 if cmd[0] == "scan" else 0

    assert run_once(JOB, runner=fake, log=logs.append) == 1
    assert [c[0] for c in calls] == ["quota", "scan"]
    assert any("scan" in line and "falló" in line for line in logs)


def test_fixture_commands_skip_quota_check() -> None:
    assert fixture_commands(Path("f.json")) == [
        ["scan", "--from-fixture", "f.json", "--yes"],
        ["rank"],
        ["export", "--format", "csv"],
    ]


def test_run_from_fixture_stops_on_failed_step() -> None:
    calls: list[list[str]] = []
    logs: list[str] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 1 if cmd[0] == "rank" else 0

    assert run_from_fixture(Path("f.json"), runner=fake, log=logs.append) == 1
    assert [c[0] for c in calls] == ["scan", "rank"]
    assert any("0 consultas" in line for line in logs)
    assert any("rank" in line and "falló" in line for line in logs)


def test_run_forever_waits_until_each_time() -> None:
    clock = [datetime(2026, 11, 9, 12, 0, tzinfo=UTC)]  # 09:00 en Chile
    slept: list[float] = []
    runs: list[datetime] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    def fake(cmd: list[str]) -> int:
        if cmd[0] == "quota":
            runs.append(clock[0])
        return 0

    run_forever(
        JOB,
        TIMES,
        runner=fake,
        log=lambda _: None,
        sleep=sleep,
        clock=lambda: clock[0],
        max_runs=3,
    )
    assert runs == [
        datetime(2026, 11, 9, 13, 0, tzinfo=UTC),  # 10:00 CL
        datetime(2026, 11, 9, 21, 0, tzinfo=UTC),  # 18:00 CL
        datetime(2026, 11, 10, 13, 0, tzinfo=UTC),  # 10:00 CL del día siguiente
    ]
    assert slept[0] == 3600


def test_schedule_once_runs_immediately(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("odds_scanner.cli._run_subcommand", lambda cmd: calls.append(cmd) or 0)
    result = runner.invoke(app, ["schedule", "--once", "--sports", "soccer_epl"])
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["quota", "scan", "rank", "export"]
    log = (workdir / "data" / "scheduler.log").read_text(encoding="utf-8")
    assert "corrida completa" in log


def test_run_subcommand_spawns_real_process(capfd: pytest.CaptureFixture[str]) -> None:
    """Integración: `python -m odds_scanner version` en un proceso hijo (sin red)."""
    from odds_scanner import __version__
    from odds_scanner.cli import _run_subcommand

    assert _run_subcommand(["version"]) == 0
    assert __version__ in capfd.readouterr().out
    assert _run_subcommand(["comando-que-no-existe"]) != 0


# --- tope mensual del programador ---------------------------------------------

NOV = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)


def ok_runner(calls: list[list[str]]) -> Callable[[list[str]], int]:
    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 0

    return fake


def test_month_key_uses_chile_time() -> None:
    assert month_key(datetime(2026, 12, 1, 2, 0, tzinfo=UTC)) == "2026-11"  # 23:00 del 30-11 CL
    assert month_key(datetime(2026, 12, 1, 4, 0, tzinfo=UTC)) == "2026-12"


def test_budget_records_each_scan(tmp_path: Path) -> None:
    budget = MonthlyBudget(tmp_path / "b.json", limit=5)
    calls: list[list[str]] = []
    logs: list[str] = []
    assert run_once(JOB, runner=ok_runner(calls), log=logs.append, budget=budget, now=NOV) == 0
    assert budget.spent("2026-11") == JOB.cost
    assert any("tope mensual" in line for line in logs)


def test_budget_blocks_before_calling_api(tmp_path: Path) -> None:
    budget = MonthlyBudget(tmp_path / "b.json", limit=JOB.cost)
    budget.add("2026-11", 1)
    calls: list[list[str]] = []
    logs: list[str] = []
    code = run_once(JOB, runner=ok_runner(calls), log=logs.append, budget=budget, now=NOV)
    assert code == BUDGET_EXHAUSTED
    assert calls == []  # ni siquiera verifica la cuota
    assert any("tope mensual alcanzado" in line for line in logs)


def test_budget_zero_blocks_everything(tmp_path: Path) -> None:
    budget = MonthlyBudget(tmp_path / "b.json", limit=0)
    calls: list[list[str]] = []
    code = run_once(JOB, runner=ok_runner(calls), log=lambda _: None, budget=budget, now=NOV)
    assert code == BUDGET_EXHAUSTED and calls == []


def test_budget_resets_each_month_and_survives_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    budget = MonthlyBudget(path, limit=10)
    budget.add("2026-10", 10)
    assert budget.spent("2026-10") == 10
    assert budget.spent("2026-11") == 0
    assert budget.allows("2026-11", 10) is True
    path.write_text("no es json", encoding="utf-8")
    assert budget.spent("2026-11") == 0
    path.write_text(json.dumps({"month": "2026-11", "spent": -3}), encoding="utf-8")
    assert budget.spent("2026-11") == 0
    path.write_text(json.dumps(["2026-11"]), encoding="utf-8")
    assert budget.spent("2026-11") == 0


def test_budget_counts_failed_scan(tmp_path: Path) -> None:
    budget = MonthlyBudget(tmp_path / "b.json", limit=10)

    def fake(cmd: list[str]) -> int:
        return 1 if cmd[0] == "scan" else 0

    assert run_once(JOB, runner=fake, log=lambda _: None, budget=budget, now=NOV) == 1
    assert budget.spent("2026-11") == JOB.cost


def test_budget_not_spent_when_quota_insufficient(tmp_path: Path) -> None:
    budget = MonthlyBudget(tmp_path / "b.json", limit=10)
    code = run_once(JOB, runner=lambda _: 2, log=lambda _: None, budget=budget, now=NOV)
    assert code == 2
    assert budget.spent("2026-11") == 0


def test_budget_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MonthlyBudget(tmp_path / "b.json", limit=-1)
    with pytest.raises(ValueError, match="now"):
        run_once(JOB, runner=lambda _: 0, log=lambda _: None, budget=MonthlyBudget(tmp_path, 1))


def test_run_forever_stops_calling_api_when_budget_runs_out(tmp_path: Path) -> None:
    clock = [datetime(2026, 11, 9, 12, 0, tzinfo=UTC)]
    budget = MonthlyBudget(tmp_path / "b.json", limit=2 * JOB.cost)
    calls: list[list[str]] = []

    def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    run_forever(
        JOB,
        TIMES,
        runner=ok_runner(calls),
        log=lambda _: None,
        sleep=sleep,
        clock=lambda: clock[0],
        max_runs=4,
        budget=budget,
    )
    assert sum(1 for c in calls if c[0] == "scan") == 2
    assert budget.spent("2026-11") == 2 * JOB.cost


def test_schedule_once_respects_monthly_budget(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("odds_scanner.cli._run_subcommand", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setenv("SCHEDULE_MONTHLY_BUDGET", "0")
    result = runner.invoke(app, ["schedule", "--once", "--sports", "soccer_epl"])
    assert result.exit_code == BUDGET_EXHAUSTED, result.output
    assert calls == []
    log = (workdir / "data" / "scheduler.log").read_text(encoding="utf-8")
    assert "tope mensual alcanzado" in log
