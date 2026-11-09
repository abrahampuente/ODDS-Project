"""Tests de la CLI (version y doctor)."""

from pathlib import Path

from typer.testing import CliRunner

from odds_scanner import __version__
from odds_scanner.cli import app
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
