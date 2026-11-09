"""Tests del cliente HTTP. Nunca tocan la red: todo pasa por respx."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from odds_scanner.client import (
    BASE_URL,
    Event,
    InvalidApiKeyError,
    OddsApiError,
    OddsClient,
    QuotaGuardError,
    estimate_cost,
    read_quota,
)
from odds_scanner.config import load_settings
from tests.conftest import FAKE_KEY

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"
ODDS_FIXTURES = sorted(p for p in FIXTURES.glob("*.json") if "sports" not in p.name)
SPORTS_URL = f"{BASE_URL}/v4/sports"
EPL_URL = f"{BASE_URL}/v4/sports/soccer_epl/odds"

QUOTA_HEADERS = {"x-requests-remaining": "480", "x-requests-used": "20", "x-requests-last": "1"}


class FakeClock:
    """Reloj controlable para probar TTL y timestamps."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Sleeps:
    """Registra las esperas del backoff sin dormir de verdad."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, sleeps: Sleeps) -> OddsClient:
    return OddsClient(
        FAKE_KEY,
        data_dir=tmp_path / "data",
        fixtures_dir=tmp_path / "fixtures",
        min_quota_reserve=50,
        sleep=sleeps,
        clock=clock,
    )


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def seed_quota(data_dir: Path, remaining: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "quota.json").write_text(
        json.dumps(
            {"remaining": remaining, "used": 0, "last": 0, "updated_at": "2026-11-09T00:00:00Z"}
        ),
        encoding="utf-8",
    )


# --- estimate_cost ------------------------------------------------------------


@pytest.mark.parametrize(
    ("regions", "markets", "cost"),
    [
        (["eu"], ["h2h"], 1),
        (["eu"], ["h2h", "spreads", "totals"], 3),
        (["eu", "uk", "us"], ["h2h"], 3),
        (["eu", "uk", "us"], ["h2h", "spreads", "totals"], 9),
        (["eu", "eu"], ["h2h", "h2h"], 1),  # duplicados no cuentan doble
    ],
)
def test_estimate_cost_matches_docs(regions: list[str], markets: list[str], cost: int) -> None:
    assert estimate_cost(regions, markets) == cost


@pytest.mark.parametrize(("regions", "markets"), [([], ["h2h"]), (["eu"], []), ([" "], ["h2h"])])
def test_estimate_cost_rejects_empty(regions: list[str], markets: list[str]) -> None:
    with pytest.raises(ValueError):
        estimate_cost(regions, markets)


# --- parseo -------------------------------------------------------------------


@pytest.mark.parametrize("path", ODDS_FIXTURES, ids=lambda p: p.name)
def test_parse_odds_fixture(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    events = [Event.model_validate(e) for e in raw]
    assert len(events) == len(raw)
    for event in events:
        assert event.home_team and event.away_team
        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                assert market.outcomes
                assert all(o.price > 1.0 for o in market.outcomes)


def test_get_odds_parses_sample_fixture(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        respx.get(EPL_URL).respond(200, json=load_fixture("sample_odds_h2h.json"))
        events = client.get_odds("soccer_epl", ["eu"], ["h2h"])
    assert len(events) == 2
    first = events[0]
    assert (first.home_team, first.away_team) == ("Arsenal", "Chelsea")
    assert first.commence_time == datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    pinnacle = first.bookmakers[0]
    assert pinnacle.key == "pinnacle"
    h2h = pinnacle.markets[0]
    assert h2h.key == "h2h"
    assert [o.price for o in h2h.outcomes] == [2.10, 3.60, 3.40]
    assert events[1].bookmakers == []


def test_get_odds_sends_documented_params(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        client.get_odds("soccer_epl", ["eu", "uk"], ["h2h"])
    params = route.calls.last.request.url.params
    assert params["regions"] == "eu,uk"
    assert params["markets"] == "h2h"
    assert params["oddsFormat"] == "decimal"
    assert params["dateFormat"] == "iso"
    assert params["apiKey"] == FAKE_KEY


def test_list_sports(client: OddsClient) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL).respond(
            200, json=load_fixture("sample_sports.json"), headers=QUOTA_HEADERS
        )
        sports = client.list_sports()
    assert [s.key for s in sports][:2] == ["soccer_epl", "soccer_chile_campeonato"]
    assert sports[2].has_outrights is True
    assert "all" not in route.calls.last.request.url.params


# --- headers de cuota ---------------------------------------------------------


def test_quota_headers_written_to_quota_json(
    client: OddsClient, tmp_path: Path, clock: FakeClock
) -> None:
    with respx.mock:
        respx.get(SPORTS_URL).respond(200, json=[], headers=QUOTA_HEADERS)
        client.list_sports()
    raw = json.loads((tmp_path / "data" / "quota.json").read_text(encoding="utf-8"))
    assert raw["remaining"] == 480
    assert raw["used"] == 20
    assert raw["last"] == 1
    assert raw["updated_at"] == clock.now.isoformat()
    quota = read_quota(tmp_path / "data")
    assert quota is not None and quota.remaining == 480


def test_quota_json_untouched_without_headers(client: OddsClient, tmp_path: Path) -> None:
    with respx.mock:
        respx.get(SPORTS_URL).respond(200, json=[])
        client.list_sports()
    assert read_quota(tmp_path / "data") is None


def test_read_quota_ignores_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / "quota.json").write_text("{no es json", encoding="utf-8")
    assert read_quota(tmp_path) is None


# --- guardia de cuota ---------------------------------------------------------


def test_quota_guard_blocks_without_calling(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 51)  # 51 - 2 = 49 < 50
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        with pytest.raises(QuotaGuardError, match="49"):
            client.get_odds("soccer_epl", ["eu", "uk"], ["h2h"])
    assert not route.called


def test_quota_guard_allows_exact_reserve(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 52)  # 52 - 2 = 50, no es < 50
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        client.get_odds("soccer_epl", ["eu", "uk"], ["h2h"])
    assert route.called


def test_quota_guard_blocks_when_quota_unknown(client: OddsClient) -> None:
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        with pytest.raises(QuotaGuardError, match="sports"):
            client.get_odds("soccer_epl", ["eu"], ["h2h"])
    assert not route.called


def test_free_endpoint_not_blocked_by_low_quota(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 10)
    with respx.mock:
        respx.get(SPORTS_URL).respond(200, json=[])
        assert client.list_sports() == []


# --- caché --------------------------------------------------------------------


def test_cache_hit_within_ttl_skips_network(
    client: OddsClient, tmp_path: Path, clock: FakeClock
) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=load_fixture("sample_odds_h2h.json"))
        first = client.get_odds("soccer_epl", ["eu"], ["h2h"])
        assert client.last_from_cache is False
        clock.now += timedelta(minutes=9, seconds=59)
        second = client.get_odds("soccer_epl", ["eu"], ["h2h"])
        assert client.last_from_cache is True
    assert route.call_count == 1
    assert first == second
    assert list((tmp_path / "data" / "cache").glob("*.json"))


def test_cache_expires_after_ttl(client: OddsClient, tmp_path: Path, clock: FakeClock) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        client.get_odds("soccer_epl", ["eu"], ["h2h"])
        clock.now += timedelta(minutes=10, seconds=1)
        client.get_odds("soccer_epl", ["eu"], ["h2h"])
    assert route.call_count == 2


def test_cache_key_depends_on_params(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        route = respx.get(EPL_URL).respond(200, json=[])
        client.get_odds("soccer_epl", ["eu"], ["h2h"])
        client.get_odds("soccer_epl", ["uk"], ["h2h"])
    assert route.call_count == 2


def test_cache_hit_not_blocked_by_quota_guard(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        respx.get(EPL_URL).respond(200, json=[])
        client.get_odds("soccer_epl", ["eu"], ["h2h"])
    seed_quota(tmp_path / "data", 10)
    assert client.get_odds("soccer_epl", ["eu"], ["h2h"]) == []  # sale del caché, gratis


# --- errores y reintentos -----------------------------------------------------


def test_401_raises_clear_key_error_without_retry(client: OddsClient, sleeps: Sleeps) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL).respond(
            401, json={"message": "API key is not valid", "error_code": "INVALID_KEY"}
        )
        with pytest.raises(InvalidApiKeyError, match="ODDS_API_KEY") as exc:
            client.list_sports()
    assert route.call_count == 1
    assert sleeps.calls == []
    assert FAKE_KEY not in str(exc.value)


def test_429_is_retried_then_succeeds(client: OddsClient, sleeps: Sleeps) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL)
        route.side_effect = [
            httpx.Response(429, json={"message": "Too many requests"}),
            httpx.Response(200, json=load_fixture("sample_sports.json")),
        ]
        sports = client.list_sports()
    assert len(sports) == 3
    assert route.call_count == 2
    assert sleeps.calls == [1.0]


def test_429_honours_retry_after(client: OddsClient, sleeps: Sleeps) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, json=[]),
        ]
        client.list_sports()
    assert sleeps.calls == [5.0]


def test_500_exhausts_retries(client: OddsClient, sleeps: Sleeps) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL).respond(500, text="Internal Server Error")
        with pytest.raises(OddsApiError, match="500") as exc:
            client.list_sports()
    assert route.call_count == 4  # 1 intento + 3 reintentos
    assert sleeps.calls == [1.0, 2.0, 4.0]
    assert FAKE_KEY not in str(exc.value)


def test_network_error_is_retried_and_sanitized(client: OddsClient, sleeps: Sleeps) -> None:
    with respx.mock:
        route = respx.get(SPORTS_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(OddsApiError, match="red") as exc:
            client.list_sports()
    assert route.call_count == 4
    assert FAKE_KEY not in str(exc.value)


def test_422_raises_with_api_message(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        route = respx.get(EPL_URL).respond(
            422, json={"message": "Invalid markets", "error_code": "INVALID_MARKET"}
        )
        with pytest.raises(OddsApiError, match="Invalid markets"):
            client.get_odds("soccer_epl", ["eu"], ["nope"])
    assert route.call_count == 1


def test_unexpected_payload_raises(client: OddsClient) -> None:
    with respx.mock:
        respx.get(SPORTS_URL).respond(200, text="<html>no json</html>")
        with pytest.raises(OddsApiError, match="JSON"):
            client.list_sports()


# --- --record y fugas de la key -----------------------------------------------


def test_record_writes_raw_response_without_key(
    client: OddsClient, tmp_path: Path, clock: FakeClock
) -> None:
    seed_quota(tmp_path / "data", 500)
    raw = load_fixture("sample_odds_h2h.json")
    with respx.mock:
        respx.get(EPL_URL).respond(200, json=raw)
        client.get_odds("soccer_epl", ["eu"], ["h2h"], record=True)
    path = tmp_path / "fixtures" / "soccer_epl_2026-11-09.json"
    assert client.last_record_path == path
    assert json.loads(path.read_text(encoding="utf-8")) == raw


def test_record_scrubs_key_if_echoed_in_body(client: OddsClient, tmp_path: Path) -> None:
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        leaky = [{**load_fixture("sample_odds_h2h.json")[0], "sport_title": FAKE_KEY}]  # type: ignore[index]
        respx.get(EPL_URL).respond(200, json=leaky)
        client.get_odds("soccer_epl", ["eu"], ["h2h"], record=True)
    assert client.last_record_path is not None
    assert FAKE_KEY not in client.last_record_path.read_text(encoding="utf-8")


def _all_text_under(*roots: Path) -> str:
    chunks = []
    for root in roots:
        if root.exists():
            chunks += [p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()]
    return "\n".join(chunks)


def test_api_key_never_in_files_or_logs(
    client: OddsClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seed_quota(tmp_path / "data", 500)
    with respx.mock:
        respx.get(SPORTS_URL).respond(200, json=[], headers=QUOTA_HEADERS)
        respx.get(EPL_URL).respond(
            200, json=load_fixture("sample_odds_h2h.json"), headers=QUOTA_HEADERS
        )
        client.list_sports()
        client.get_odds("soccer_epl", ["eu"], ["h2h"], record=True)
        client.get_odds("soccer_epl", ["eu"], ["h2h"])  # caché
    assert caplog.records  # el cliente sí registra actividad
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in _all_text_under(tmp_path / "data", tmp_path / "fixtures")


def test_real_key_not_in_repo_fixtures() -> None:
    """Grep de la key real (si hay .env) y de la ficticia en fixtures/ del repo."""
    keys = [FAKE_KEY]
    real = load_settings(env_file=REPO_ROOT / ".env").odds_api_key
    if real is not None and real.get_secret_value():
        keys.append(real.get_secret_value())
    text = _all_text_under(FIXTURES)
    for key in keys:
        assert key not in text
