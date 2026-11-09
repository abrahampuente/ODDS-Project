"""Tests de carga de configuración."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from odds_scanner.config import ConfigError, is_valid_api_key, load_settings
from tests.conftest import FAKE_KEY


def test_import_does_not_require_key(tmp_path: Path) -> None:
    """Importar config en un proceso limpio, sin key ni .env, no debe fallar."""
    env = {k: v for k, v in os.environ.items() if k != "ODDS_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", "import odds_scanner.config, odds_scanner.cli"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_settings_without_key_loads_but_require_fails(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / ".env")  # archivo inexistente
    assert settings.odds_api_key is None
    assert settings.min_quota_reserve == 50
    with pytest.raises(ConfigError, match=r"ODDS_API_KEY.*\.env"):
        settings.require_api_key()


def test_settings_with_empty_key_fails_on_require(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(env_file=env).require_api_key()


def test_settings_with_key_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"ODDS_API_KEY={FAKE_KEY}\nMIN_QUOTA_RESERVE=75\n", encoding="utf-8")
    settings = load_settings(env_file=env)
    assert settings.require_api_key() == FAKE_KEY
    assert settings.min_quota_reserve == 75


def test_settings_with_key_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", FAKE_KEY)
    assert load_settings(env_file=tmp_path / ".env").require_api_key() == FAKE_KEY


def test_key_is_not_exposed_in_repr(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"ODDS_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    settings = load_settings(env_file=env)
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)


def test_negative_quota_reserve_is_rejected(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MIN_QUOTA_RESERVE=-1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(env_file=env)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (FAKE_KEY, True),
        (FAKE_KEY.upper(), True),
        ("", False),
        ("abc", False),
        (FAKE_KEY + "0", False),
        ("g" * 32, False),
        (" " + FAKE_KEY[1:], False),
    ],
)
def test_is_valid_api_key(key: str, expected: bool) -> None:
    assert is_valid_api_key(key) is expected
