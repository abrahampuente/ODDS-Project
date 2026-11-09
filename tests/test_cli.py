"""Tests de la CLI (version y doctor)."""

import json
import re
from pathlib import Path

import pytest
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


# --- rank (fase 4) ------------------------------------------------------------


def test_rank_from_real_fixture_prints_top_15() -> None:
    result = runner.invoke(
        app, ["rank", "--from-fixture", str(FIXTURES / "soccer_epl_2026-11-09.json")]
    )
    assert result.exit_code == 0, result.output
    assert "Top 15" in result.output
    assert "Descartadas: 27 (lay_market=27)" in result.output
    assert result.output.count("h2h") >= 15


def test_rank_warns_on_arbitrage() -> None:
    result = runner.invoke(
        app, ["rank", "--from-fixture", str(FIXTURES / "synthetic_arbitrage.json")]
    )
    assert result.exit_code == 0, result.output
    table_rows = [line for line in result.output.splitlines() if "Jugador A - Jugador B" in line]
    assert len(table_rows) == 2
    assert all(" ARB" in line for line in table_rows)  # marca en la fila de la tabla
    assert "desactualizada" in result.output


def test_rank_table_fits_120_columns() -> None:
    result = runner.invoke(
        app, ["rank", "--from-fixture", str(FIXTURES / "soccer_epl_2026-11-09.json")]
    )
    rows = [line for line in result.output.splitlines() if " h2h " in line]
    assert len(rows) == 15
    assert all(len(line.rstrip()) <= 120 for line in rows)
    assert all(" ARB" in line for line in rows[:3])  # Chelsea-Leeds


def test_rank_applies_odds_filter() -> None:
    result = runner.invoke(
        app,
        [
            "rank",
            "--from-fixture",
            str(FIXTURES / "synthetic_arbitrage.json"),
            "--min-odds",
            "2.15",
            "--max-odds",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    table_rows = [line for line in result.output.splitlines() if "Jugador A - Jugador B" in line]
    assert len(table_rows) == 1  # solo Jugador A @ 2.20 cae en [2.15, 3]
    assert "casa_uno" in table_rows[0] and "2.20" in table_rows[0]
    assert "casa_dos" not in table_rows[0]


def test_rank_missing_fixture_fails() -> None:
    result = runner.invoke(app, ["rank", "--from-fixture", "no/existe.json"])
    assert result.exit_code == 1
    assert "no existe" in result.output


def test_rank_invalid_filters_fail() -> None:
    result = runner.invoke(
        app,
        [
            "rank",
            "--from-fixture",
            str(FIXTURES / "synthetic_arbitrage.json"),
            "--min-odds",
            "5",
            "--max-odds",
            "4",
        ],
    )
    assert result.exit_code == 1


# --- vista value (fase 5) -----------------------------------------------------

EV_DISCLAIMER_TEXT = (
    "EV estimado con consenso de mercado. No garantiza ganancias; un EV "
    "positivo aislado suele ser una cuota desactualizada."
)


def _rank_output(*extra: str) -> str:
    result = runner.invoke(
        app, ["rank", "--from-fixture", str(FIXTURES / "soccer_epl_2026-11-09.json"), *extra]
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize("view", ["margin", "value"])
def test_rank_always_prints_ev_disclaimer(view: str) -> None:
    output = _rank_output("--view", view)
    assert " ".join(EV_DISCLAIMER_TEXT.split()) in " ".join(output.split())


def test_rank_margin_view_has_ev_columns() -> None:
    header = next(line for line in _rank_output().splitlines() if "cuota" in line)
    for column in ("n", "p.justa", "EV", "etiqueta"):
        assert column in header


def test_rank_value_view_sorted_by_ev() -> None:
    output = _rank_output("--view", "value", "--top", "60")
    rows = [line for line in output.splitlines() if " h2h " in line]
    assert len(rows) == 60
    assert all(len(line.rstrip()) <= 120 for line in rows)
    evs = [float(re.findall(r"([+-]\d+\.\d+)%", line)[-1]) for line in rows]
    assert evs == sorted(evs, reverse=True)
    assert "por EV" in output
    assert any(label in rows[0] for label in ("revisar", "neutro", "evitar"))


def test_rank_value_view_reports_insufficient() -> None:
    result = runner.invoke(
        app,
        ["rank", "--from-fixture", str(FIXTURES / "synthetic_arbitrage.json"), "--view", "value"],
    )
    assert result.exit_code == 0, result.output
    assert "datos insuficientes" in result.output
    assert "Top 0" in result.output


def test_rank_rejects_unknown_view() -> None:
    result = runner.invoke(
        app, ["rank", "--from-fixture", str(FIXTURES / "synthetic_arbitrage.json"), "--view", "x"]
    )
    assert result.exit_code != 0
