"""Fixtures compartidas: aíslan los tests del entorno real del usuario."""

from collections.abc import Iterator
from pathlib import Path

import pytest

# Formato válido de The Odds API (32 hex). Valor ficticio, no es una key real.
FAKE_KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Evita que variables reales del sistema contaminen los tests."""
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("MIN_QUOTA_RESERVE", raising=False)
    yield


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Directorio de trabajo temporal con data/ creado."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path
