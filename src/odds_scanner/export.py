"""Exportación de ranking, apuestas y pozos a CSV (un archivo por hoja) o xlsx."""

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from odds_scanner.ledger import Bet, Pot
from odds_scanner.rank import RankedOutcome

Sheet = tuple[list[str], list[list[Any]]]

RANKING_COLUMNS = [
    "posicion",
    "inicio_utc",
    "inicio_chile",
    "deporte",
    "evento",
    "mercado",
    "linea",
    "resultado",
    "punto",
    "mejor_cuota",
    "casa",
    "cuota_publicada",
    "comision",
    "margen_casa",
    "margen_combinado",
    "arbitraje",
    "n_casas",
    "prob_justa",
    "ev",
    "etiqueta",
    "sharp_casa",
    "sharp_prob",
]
BET_COLUMNS = [
    "id",
    "pozo",
    "stake",
    "cuota",
    "evento",
    "mercado",
    "seleccion",
    "casa",
    "fecha",
    "estado",
    "pago",
    "liquidada",
    "margen_casa",
    "prob_justa",
    "ev",
    "origen",
    "notas",
]
POT_COLUMNS = ["id", "inicial", "saldo", "estado"]


def build_sheets(
    ranking: Sequence[RankedOutcome], bets: Sequence[Bet], pots: Sequence[Pot]
) -> dict[str, Sheet]:
    """Arma las hojas "ranking", "apuestas" y "pozos" (encabezado + filas)."""
    ranking_rows: list[list[Any]] = [
        [
            i,
            r.commence_time_utc.isoformat(),
            r.commence_time_cl.isoformat(),
            r.sport,
            f"{r.home} vs {r.away}",
            r.market,
            r.line,
            r.outcome,
            r.point,
            r.best_price,
            r.bookmaker,
            r.gross_price,
            r.commission,
            r.book_margin,
            r.best_market_margin,
            r.is_arbitrage,
            r.n_books,
            r.fair_prob,
            r.ev,
            r.label,
            r.sharp_book,
            r.sharp_prob,
        ]
        for i, r in enumerate(ranking, start=1)
    ]
    bet_rows: list[list[Any]] = [
        [
            b.id,
            ",".join(map(str, b.pot_ids)),
            b.stake,
            b.odds,
            b.event,
            b.market,
            b.selection,
            b.bookmaker,
            b.placed_at,
            b.status,
            b.payout,
            b.settled_at,
            b.book_margin,
            b.fair_prob,
            b.ev,
            b.source,
            b.notes,
        ]
        for b in bets
    ]
    pot_rows: list[list[Any]] = [[p.id, p.initial, p.balance, p.status] for p in pots]
    return {
        "ranking": (RANKING_COLUMNS, ranking_rows),
        "apuestas": (BET_COLUMNS, bet_rows),
        "pozos": (POT_COLUMNS, pot_rows),
    }


def export_csv(out_dir: Path, sheets: dict[str, Sheet]) -> list[Path]:
    """Un CSV por hoja (UTF-8 con BOM para que Excel respete los acentos)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (header, rows) in sheets.items():
        path = out_dir / f"{name}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(["" if v is None else v for v in row] for row in rows)
        paths.append(path)
    return paths


def export_xlsx(path: Path, sheets: dict[str, Sheet]) -> Path:
    """Un libro con una hoja por tabla; encabezado en negrita y fila congelada."""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)  # type: ignore[arg-type]
    for name, (header, rows) in sheets.items():
        sheet = workbook.create_sheet(title=name)
        sheet.append(header)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
    workbook.save(path)
    return path
