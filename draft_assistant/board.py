"""Board data loading and player-name normalization."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import DraftState, Player, Tag


_NAME_ALIASES = {
    # Sleeper's active player row uses this spelling; the analyst sources and
    # approved board use Jonathan. Keep identity reconciliation at the data
    # boundary so policy logic never has to special-case UI spelling.
    "jonathonbrooks": "jonathanbrooks",
}

# Search must use Sleeper's rendered spelling; its player finder does not
# fuzzy-match the canonical analyst-board spelling.
_SLEEPER_SEARCH_NAMES = {
    "jonathanbrooks": "Jonathon Brooks",
}


def normalize_name(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.casefold())


def canonical_name_key(name: str) -> str:
    """Normalize a player name and reconcile a verified platform alias."""

    key = normalize_name(name)
    return _NAME_ALIASES.get(key, key)


def sleeper_search_name(name: str) -> str:
    """Return the exact Sleeper player-finder spelling for a board name."""

    return _SLEEPER_SEARCH_NAMES.get(canonical_name_key(name), name)


@dataclass(frozen=True)
class Board:
    meta: dict[str, str]
    players: tuple[Player, ...]
    roster_targets: dict[str, int]
    avoid_rules: dict[str, str]
    pick_plan: dict[str, str]
    source_tags: dict[str, Tag]
    personal_tags: dict[str, Tag]
    preference_adjustments: dict[str, dict[str, Any]]
    rsp_adjustments: dict[str, dict[str, Any]]
    harmon_wr_adjustments: dict[str, dict[str, Any]]
    harmon_rookie_wr_adjustments: dict[str, dict[str, Any]]

    @property
    def by_normalized_name(self) -> dict[str, Player]:
        index = {canonical_name_key(player.name): player for player in self.players}
        for alias, canonical in _NAME_ALIASES.items():
            if canonical in index:
                index[alias] = index[canonical]
        return index

    def available(self, state: DraftState) -> list[Player]:
        drafted = {canonical_name_key(name) for name in state.drafted_players}
        allowed = (
            {canonical_name_key(name) for name in state.available_players}
            if state.available_players is not None
            else None
        )
        return [
            player
            for player in self.players
            if player.draftable
            and canonical_name_key(player.name) not in drafted
            and (allowed is None or canonical_name_key(player.name) in allowed)
        ]

    def positions_for(self, names: Iterable[str]) -> list[str]:
        index = self.by_normalized_name
        return [index[key].position for key in map(canonical_name_key, names) if key in index]

    def rsp_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.rsp_adjustments.get(canonical_name_key(player_name))

    def harmon_wr_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.harmon_wr_adjustments.get(canonical_name_key(player_name))

    def harmon_rookie_wr_adjustment(self, player_name: str) -> dict[str, Any] | None:
        return self.harmon_rookie_wr_adjustments.get(canonical_name_key(player_name))

    def source_tag(self, player_name: str) -> Tag:
        """Original JJ-board signal; informative, never an automatic gate."""

        return self.source_tags.get(canonical_name_key(player_name), Tag.NONE)

    def personal_tag(self, player_name: str) -> Tag:
        """User-directed risk/news gate, which takes precedence over scoring."""

        return self.personal_tags.get(canonical_name_key(player_name), Tag.NONE)

    def preference_adjustment(self, player_name: str) -> dict[str, Any] | None:
        """Return a user-directed rank overlay, independent of tags.

        A positive number of slots is a GLAZE (rank the player higher); a
        negative number is a SHADE (rank the player lower).  These are rank
        moves, not eligibility gates or analyst-source edits.
        """

        return self.preference_adjustments.get(canonical_name_key(player_name))


def load_board(path: Path) -> Board:
    raw = json.loads(path.read_text(encoding="utf-8"))
    personal_tags = {
        normalize_name(name): Tag(value)
        for name, value in raw.get("personal_tags", {}).items()
    }
    source_tags: dict[str, Tag] = {}
    players: list[Player] = []
    for position, rows in raw["players"].items():
        for rank, (name, tag) in enumerate(rows, start=1):
            source_tags[normalize_name(name)] = Tag(tag)
            players.append(
                Player(
                    name=name,
                    position=position,
                    rank=rank,
                    # Explicit personal instructions are gates. A source
                    # CHECK_NEWS label is also a gate because it requires a
                    # live verification; the remaining JJ tags are weighted
                    # analyst evidence, stored separately below.
                    tag=personal_tags.get(
                        normalize_name(name),
                        Tag.CHECK_NEWS if Tag(tag) == Tag.CHECK_NEWS else Tag.NONE,
                    ),
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
        source_tags=source_tags,
        personal_tags=personal_tags,
        preference_adjustments={
            normalize_name(name): {
                "slots": int(details["slots"]),
                "label": str(details["label"]).upper(),
            }
            for name, details in raw.get("preference_adjustments", {}).items()
        },
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
