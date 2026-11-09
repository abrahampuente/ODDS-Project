"""Persistencia en disco de capturas de cuotas y del último ranking mostrado.

- data/captures/capture_<UTC>.json: eventos de un `scan` (para rank/value y CLV).
- data/last_rank.json: filas del último ranking (para `bet --row N`).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from odds_scanner.client import Event
from odds_scanner.normalize import normalize_events
from odds_scanner.rank import RankedOutcome, summarize_markets

DEFAULT_DATA_DIR = Path("data")
CAPTURES_DIR = "captures"
LAST_RANK_FILE = "last_rank.json"


@dataclass(frozen=True)
class Capture:
    """Una captura de cuotas."""

    path: Path
    captured_at: datetime
    sports: list[str]
    markets: list[str]
    regions: list[str]
    events: list[Event]


@dataclass(frozen=True)
class LastRank:
    """Último ranking mostrado (base de `bet --row N`)."""

    created_at: datetime
    view: str
    source: str
    rows: list[RankedOutcome]


def save_capture(
    data_dir: Path,
    events: Sequence[Event],
    *,
    sports: Sequence[str],
    markets: Sequence[str],
    regions: Sequence[str],
    captured_at: datetime,
) -> Path:
    """Guarda una captura y devuelve su ruta."""
    folder = data_dir / CAPTURES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"capture_{stamp}.json"
    payload = {
        "captured_at": captured_at.isoformat(),
        "sports": list(sports),
        "markets": list(markets),
        "regions": list(regions),
        "events": [e.model_dump(mode="json") for e in events],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_capture(path: Path) -> Capture:
    """Lee una captura; ValueError si el archivo no es válido."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Capture(
            path=path,
            captured_at=datetime.fromisoformat(raw["captured_at"]),
            sports=list(raw["sports"]),
            markets=list(raw["markets"]),
            regions=list(raw["regions"]),
            events=[Event.model_validate(e) for e in raw["events"]],
        )
    except (OSError, KeyError, TypeError, ValidationError) as exc:
        raise ValueError(f"captura inválida: {path.name}") from exc


def list_captures(data_dir: Path) -> list[Capture]:
    """Capturas válidas ordenadas por fecha (las corruptas se ignoran)."""
    captures = []
    for path in sorted((data_dir / CAPTURES_DIR).glob("capture_*.json")):
        try:
            captures.append(load_capture(path))
        except ValueError:
            continue
    return sorted(captures, key=lambda c: c.captured_at)


def latest_capture(data_dir: Path) -> Capture | None:
    """La captura más reciente, o None si no hay."""
    captures = list_captures(data_dir)
    return captures[-1] if captures else None


def save_last_rank(
    data_dir: Path,
    rows: Sequence[RankedOutcome],
    *,
    view: str,
    source: str,
    created_at: datetime,
) -> Path:
    """Guarda el ranking completo (en el orden mostrado)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / LAST_RANK_FILE
    payload = {
        "created_at": created_at.isoformat(),
        "view": view,
        "source": source,
        "rows": [r.model_dump(mode="json") for r in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_last_rank(data_dir: Path) -> LastRank | None:
    """Último ranking guardado, o None si no existe o está corrupto."""
    try:
        raw = json.loads((data_dir / LAST_RANK_FILE).read_text(encoding="utf-8"))
        return LastRank(
            created_at=datetime.fromisoformat(raw["created_at"]),
            view=str(raw["view"]),
            source=str(raw["source"]),
            rows=[RankedOutcome.model_validate(r) for r in raw["rows"]],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def closing_best_price(
    captures: Sequence[Capture],
    *,
    event_id: str,
    market: str,
    line: float | None,
    selection: str,
    commence_time: datetime,
) -> float | None:
    """Mejor cuota del resultado en la última captura previa al inicio (cierre)."""
    for capture in sorted(captures, key=lambda c: c.captured_at, reverse=True):
        if capture.captured_at > commence_time:
            continue
        events = [e for e in capture.events if e.id == event_id]
        if not events:
            continue
        for summary in summarize_markets(normalize_events(events).rows):
            if (summary.market, summary.line) != (market, line):
                continue
            try:
                return summary.best(selection).price
            except KeyError:
                return None
    return None
