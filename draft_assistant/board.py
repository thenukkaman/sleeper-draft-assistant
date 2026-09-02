"""Board data loading and player-name normalization."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import DraftState, Player, Tag


def normalize_name(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.casefold())


@dataclass(frozen=True)
class Board:
    meta: dict[str, str]
    players: tuple[Player, ...]
    roster_targets: dict[str, int]
    avoid_rules: dict[str, str]
    pick_plan: dict[str, str]
    rsp_adjustments: dict[str, dict[str, Any]]
    harmon_wr_adjustments: dict[str, dict[str, Any]]
    harmon_rookie_wr_adjustments: dict[str, dict[str, Any]]

    @property
    def by_normalized_name(self) -> dict[str, Player]:
        return {normalize_name(player.name): player for player in self.players}

    def available(self, state: DraftState) -> list[Player]:
        drafted = {normalize_name(name) for name in state.drafted_players}
        allowed = (
            {normalize_name(name) for name in state.available_players}
            if state.available_players is not None
            else None
        )
        return [
            player
            for player in self.players
            if player.draftable
            and normalize_name(player.name) not in drafted
            and (allowed is None or normalize_name(player.name) in allowed)
        ]

    def positions_for(self, names: Iterable[str]) -> list[str]:
        index = self.by_normalized_name
        return [index[key].position for key in map(normalize_name, names) if key in index]

    def rsp_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.rsp_adjustments.get(normalize_name(player_name))

    def harmon_wr_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.harmon_wr_adjustments.get(normalize_name(player_name))

    def harmon_rookie_wr_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.harmon_rookie_wr_adjustments.get(normalize_name(player_name))


def load_board(path: Path) -> Board:
    raw = json.loads(path.read_text(encoding="utf-8"))
    players: list[Player] = []
    for position, rows in raw["players"].items():
        for rank, (name, tag) in enumerate(rows, start=1):
            players.append(
                Player(
                    name=name,
                    position=position,
                    rank=rank,
                    tag=Tag(tag),
                    draftable=not name.startswith("Platform QB"),
                )
            )
    harmon_path = path.with_name("harmon_wr_2026.json")
    harmon_raw = json.loads(harmon_path.read_text(encoding="utf-8")) if harmon_path.exists() else {"players": {}}
    rookie_harmon_path = path.with_name("harmon_rookie_wr_2026.json")
    rookie_harmon_raw = json.loads(rookie_harmon_path.read_text(encoding="utf-8")) if rookie_harmon_path.exists() else {"players": {}}
    return Board(
        meta=raw["meta"],
        players=tuple(players),
        roster_targets=raw["roster_targets"],
        avoid_rules=raw["avoid_rules"],
        pick_plan=raw["pick_plan"],
        rsp_adjustments={
            normalize_name(name): dict(details)
            for name, details in raw.get("rsp_adjustments", {}).items()
        },
        harmon_wr_adjustments={
            normalize_name(name): {"rank": int(details[0]), "tier": int(details[1])}
            for name, details in harmon_raw.get("players", {}).items()
        },
        harmon_rookie_wr_adjustments={
            normalize_name(name): {
                **dict(details),
                "composite": round(
                    float(details["man"]) * 0.4 + float(details["zone"]) * 0.4 + float(details["press"]) * 0.2,
                    2,
                ),
            }
            for name, details in rookie_harmon_raw.get("players", {}).items()
        },
    )


def load_default_board() -> Board:
    return load_board(Path(__file__).with_name("board.json"))
