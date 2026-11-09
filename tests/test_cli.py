"""Tests de la CLI (version y doctor)."""

import json
from pathlib import Path

import respx
from typer.testing import CliRunner

from odds_scanner import __version__
from odds_scanner.cli import app
from odds_scanner.client import BASE_URL
from odds_scanner.doctor import check_data_dir, check_python
from tests.conftest import FAKE_KEY

runner = CliRunner()


def test_version_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_without_env_fails(workdir: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert ".env" in result.output
    assert "ODDS_API_KEY" in result.output
    assert "FALLA" in result.output


def test_doctor_with_valid_env_passes(workdir: Path) -> None:
    (workdir / ".env").write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "FALLA" not in result.output
    assert FAKE_KEY not in result.output  # nunca mostrar la key


def test_doctor_with_malformed_key_fails_without_showing_it(workdir: Path) -> None:
    bad = "not-a-real-key-123"
    (workdir / ".env").write_text(f"ODDS_API_KEY={bad}\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert bad not in result.output


def test_doctor_with_unwritable_data_dir_fails(workdir: Path) -> None:
    (workdir / ".env").write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    blocker = workdir / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--data-dir", str(blocker)])
    assert result.exit_code == 1
    assert "data" in result.output.lower()


def test_check_python_rejects_old_version() -> None:
    assert not check_python((3, 11, 9)).ok
    assert check_python((3, 12, 0)).ok


def test_check_data_dir_writable(tmp_path: Path) -> None:
    assert check_data_dir(tmp_path).ok
    assert not check_data_dir(tmp_path / "missing").ok


# --- sports / fetch (fase 3) --------------------------------------------------

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
QUOTA_HEADERS = {"x-requests-remaining": "480", "x-requests-used": "20", "x-requests-last": "1"}


def _write_env(workdir: Path) -> None:
    (workdir / ".env").write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")


def _seed_quota(workdir: Path, remaining: int) -> None:
    (workdir / "data" / "quota.json").write_text(
        json.dumps({"remaining": remaining, "used": 0, "last": 0, "updated_at": "x"}),
        encoding="utf-8",
    )


def test_sports_lists_and_shows_quota(workdir: Path) -> None:
    _write_env(workdir)
    sports = json.loads((FIXTURES / "sample_sports.json").read_text(encoding="utf-8"))
    with respx.mock:
        respx.get(f"{BASE_URL}/v4/sports").respond(200, json=sports, headers=QUOTA_HEADERS)
        result = runner.invoke(app, ["sports"])
    assert result.exit_code == 0, result.output
    assert "soccer_epl" in result.output
    assert "480" in result.output  # restantes
    assert FAKE_KEY not in result.output


def test_fetch_records_and_reports_cost(workdir: Path) -> None:
    _write_env(workdir)
    _seed_quota(workdir, 500)
    odds = json.loads((FIXTURES / "sample_odds_h2h.json").read_text(encoding="utf-8"))
    with respx.mock:
        respx.get(f"{BASE_URL}/v4/sports/soccer_epl/odds").respond(
            200, json=odds, headers=QUOTA_HEADERS
        )
        result = runner.invoke(
            app, ["fetch", "soccer_epl", "--regions", "eu", "--markets", "h2h", "--record"]
        )
    assert result.exit_code == 0, result.output
    assert "Costo estimado: 1" in result.output
    assert "Arsenal" in result.output
    recorded = list((workdir / "fixtures").glob("soccer_epl_*.json"))
    assert len(recorded) == 1
    assert FAKE_KEY not in result.output


def test_fetch_blocked_by_quota_guard(workdir: Path) -> None:
    _write_env(workdir)
    _seed_quota(workdir, 50)
    with respx.mock:
        route = respx.get(f"{BASE_URL}/v4/sports/soccer_epl/odds").respond(200, json=[])
        result = runner.invoke(app, ["fetch", "soccer_epl"])
    assert result.exit_code == 1
    assert "reserva" in result.output.lower()
    assert not route.called


def test_fetch_without_key_fails_clearly(workdir: Path) -> None:
    result = runner.invoke(app, ["fetch", "soccer_epl"])
    assert result.exit_code == 1
    assert "ODDS_API_KEY" in result.output


def test_sports_with_invalid_key_fails_clearly(workdir: Path) -> None:
    _write_env(workdir)
    with respx.mock:
        respx.get(f"{BASE_URL}/v4/sports").respond(401, json={"message": "invalid"})
        result = runner.invoke(app, ["sports"])
    assert result.exit_code == 1
    assert "ODDS_API_KEY" in result.output
