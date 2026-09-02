"""Optional specialist policy that deliberately uses Sleeper's displayed ranking."""

from __future__ import annotations

from collections import Counter

from ..board import Board
from ..models import Candidate, DraftState, Player, Recommendation, Tag
from .base import DraftPolicy


class SleeperRankedSpecialTeamsPolicy:
    """Overrides only the last-two-round K/DST choice; all other choices use its core."""

    name = "source-board-plus-sleeper-specialists-v1"

    def __init__(self, core: DraftPolicy) -> None:
        self.core = core

    def recommend(self, board: Board, state: DraftState, limit: int = 3) -> Recommendation:
        counts = Counter(position.upper() for position in state.roster_positions)
        required_position = "DEF" if state.round_number == 17 and counts["DEF"] < 1 else None
        if state.round_number == 18 and counts["K"] < 1:
            required_position = "K"
        if required_position is None:
            return self.core.recommend(board, state, limit)
        names = state.sleeper_ranked_specialists.get(required_position, ())
        if not names:
            return Recommendation(
                policy_name=self.name,
                candidates=(),
                directive=f"Draft one {required_position}, but Sleeper's current ranked list was not supplied.",
                warnings=("Browser adapter must capture the visible Sleeper specialist order before acting.",),
                requires_special_teams_policy=True,
            )
        candidates = tuple(
            Candidate(
                player=Player(name=name, position=required_position, rank=rank, tag=Tag.NONE),
                score=1000 - rank,
                reason=f"Highest currently available Sleeper-ranked {required_position}.",
            )
            for rank, name in enumerate(names[:limit], start=1)
        )
        return Recommendation(
            policy_name=self.name,
            candidates=candidates,
            directive=(
                "Round 17 DST rule: use the first available Sleeper-ranked defense."
                if required_position == "DEF"
                else "Round 18 kicker rule: use the first available Sleeper-ranked kicker."
            ),
        )
