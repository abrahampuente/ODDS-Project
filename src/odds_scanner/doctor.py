"""Chequeos de entorno para `odds-scanner doctor`. Nunca muestran la API key."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from odds_scanner.config import ConfigError, is_valid_api_key, load_settings

MIN_PYTHON = (3, 12)


@dataclass(frozen=True)
class CheckResult:
    """Resultado de un chequeo individual."""

    name: str
    ok: bool
    detail: str


def check_python(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> CheckResult:
    """Verifica que la versión de Python sea >= MIN_PYTHON."""
    shown = ".".join(str(p) for p in version)
    ok = version[:2] >= MIN_PYTHON
    need = ".".join(str(p) for p in MIN_PYTHON)
    return CheckResult("Python", ok, shown if ok else f"{shown} (se requiere >= {need})")


def check_env_file(env_file: Path) -> CheckResult:
    """Verifica que exista el archivo .env."""
    if env_file.is_file():
        return CheckResult(".env", True, f"encontrado ({env_file})")
    return CheckResult(".env", False, f"no existe {env_file}; copia .env.example a .env")


def check_api_key(env_file: Path) -> CheckResult:
    """Verifica que ODDS_API_KEY esté definida y con formato válido, sin mostrarla."""
    try:
        key = load_settings(env_file=env_file).require_api_key()
    except ConfigError:
        return CheckResult("ODDS_API_KEY", False, "no definida en .env ni en el entorno")
    except ValidationError as exc:
        return CheckResult("ODDS_API_KEY", False, f".env inválido ({exc.error_count()} errores)")
    if not is_valid_api_key(key):
        return CheckResult(
            "ODDS_API_KEY", False, f"formato inválido ({len(key)} caracteres; se esperan 32 hex)"
        )
    return CheckResult("ODDS_API_KEY", True, "formato válido (32 hex, oculta)")


def check_data_dir(data_dir: Path) -> CheckResult:
    """Verifica que data_dir exista y sea escribible creando un archivo temporal."""
    if not data_dir.is_dir():
        return CheckResult("data/", False, f"{data_dir} no existe o no es un directorio")
    try:
        with tempfile.TemporaryFile(dir=data_dir):
            pass
    except OSError as exc:
        return CheckResult("data/", False, f"{data_dir} no es escribible ({exc.strerror})")
    return CheckResult("data/", True, f"{data_dir} escribible")


def run_checks(env_file: Path, data_dir: Path) -> list[CheckResult]:
    """Ejecuta todos los chequeos en orden."""
    return [
        check_python(),
        check_env_file(env_file),
        check_api_key(env_file),
        check_data_dir(data_dir),
    ]
