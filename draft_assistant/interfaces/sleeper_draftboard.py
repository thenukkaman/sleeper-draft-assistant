"""Parse the visible Sleeper draftboard into the engine's current state.

This reads only text already shown in the browser snapshot. It intentionally
does not query private APIs or reach into Sleeper's application state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..board import Board, normalize_name


_PICK = re.compile(
    r'- generic: "(?P<label>\d+\.\d+)"\n'
    r'- generic: (?P<name>[^\n]+)\n'
    r'- generic: (?P<position>QB|RB|WR|TE|K|DEF)\b',
)
_CLOCK = re.compile(r'- generic: "(?P<label>\d+\.\d+)"\n- generic: \d{2}:\d{2}')


@dataclass(frozen=True)
class ParsedDraftboard:
    """Draft information used to refresh every decision cycle."""

    drafted_players: frozenset[str]
    roster_positions: tuple[str, ...]
    current_pick_label: str | None
    current_pick_number: int | None


def parse_visible_draftboard(
    snapshot: str, board: Board, username: str, teams: int = 12
) -> ParsedDraftboard:
    """Return source-board availability and the approved roster from Sleeper text."""

    all_picks = list(_PICK.finditer(snapshot))
    drafted = frozenset(
        full_name
        for match in all_picks
        if (full_name := _resolve_source_player(board, match["name"], match["position"]))
    )

    roster_block = _team_block(snapshot, username)
    roster_positions = tuple(match["position"] for match in _PICK.finditer(roster_block))
    clock = _CLOCK.search(snapshot)
    label = clock["label"] if clock else None
    return ParsedDraftboard(
        drafted_players=drafted,
        roster_positions=roster_positions,
        current_pick_label=label,
        current_pick_number=_overall_pick(label, teams) if label else None,
    )


def _team_block(snapshot: str, username: str) -> str:
    marker = f'heading "{username}" [level=1]'
    start = snapshot.find(marker)
    if start < 0:
        return ""
    # Sleeper renders the team heading twice; the next distinct heading begins
    # the following team card.
    following = snapshot.find('- heading "', start + len(marker))
    following = snapshot.find('- heading "', following + 1) if following >= 0 else -1
    return snapshot[start: following if following >= 0 else len(snapshot)]


def _resolve_source_player(board: Board, displayed_name: str, position: str) -> str | None:
    """Resolve Sleeper's ``J. Hurts`` form only when it is unambiguous."""

    displayed = normalize_name(displayed_name)
    candidates = []
    for player in board.players:
        if player.position != position:
            continue
        parts = re.findall(r"[a-z0-9]+", player.name.casefold())
        if len(parts) < 2:
            continue
        abbreviated = normalize_name(f"{parts[0][0]} {parts[-1]}")
        if abbreviated == displayed:
            candidates.append(player.name)
    return candidates[0] if len(candidates) == 1 else None


def _overall_pick(label: str, teams: int) -> int:
    round_text, slot_text = label.split(".")
    round_number, slot = int(round_text), int(slot_text)
    # Sleeper's visible ``round.pick`` label stays chronological within the
    # round even though its board columns reverse in snake rounds.  Thus 2.08
    # is absolute pick 20 (not 17) for a 12-team room.
    return (round_number - 1) * teams + slot
