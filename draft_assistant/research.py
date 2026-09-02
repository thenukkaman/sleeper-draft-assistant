"""Import analyst research files into policy-neutral data objects."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .board import normalize_name


@dataclass(frozen=True)
class WaldmanRedraftRecord:
    overall_rank: int
    position_rank: int
    projected_points: float
    adp: float | None
    rank_vs_adp: float | None
    upside: float


def load_waldman_redraft_csv(path: Path) -> dict[str, WaldmanRedraftRecord]:
    """Load Waldman's redraft file without embedding research in policy code."""

    records: dict[str, WaldmanRedraftRecord] = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            name = normalize_name(row["Player"])
            position_rank = _position_rank(row["Pos"])
            if not name or position_rank is None:
                continue
            records[name] = WaldmanRedraftRecord(
                overall_rank=int(row["Rank"]),
                position_rank=position_rank,
                projected_points=float(row["Points"]),
                adp=_number(row.get("ADP")),
                rank_vs_adp=_number(row.get("Rank vs ADP")),
                upside=_number(row.get("Upside")) or 0.0,
            )
    return records


def _position_rank(value: str) -> int | None:
    match = re.search(r"(\d+)$", value or "")
    return int(match.group(1)) if match else None


def _number(value: str | None) -> float | None:
    if value in {None, "", "-"}:
        return None
    try:
        return float(str(value).replace("+", ""))
    except ValueError:
        return None
