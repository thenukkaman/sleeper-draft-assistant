"""Optional specialist policy that deliberately uses Sleeper's displayed ranking."""

from __future__ import annotations

from collections import Counter

from ..board import Board
from ..models import Candidate, DraftState, Player, Recommendation, Tag
from .base import DraftPolicy
from .value_model import ValueModel


class SleeperRankedSpecialTeamsPolicy:
    """Overrides only the last-two-round K/DST choice; all other choices use its core."""

    name = "source-board-plus-sleeper-specialists-v1"

    def __init__(self, core: DraftPolicy, value_model: ValueModel | None = None) -> None:
        self.core = core
        self.value_model = value_model or ValueModel()

    def recommend(self, board: Board, state: DraftState, limit: int = 3) -> Recommendation:
        counts = Counter(position.upper() for position in state.roster_positions)
        # Default: DEF in R17 and K in R18. A defense run may pull DEF one
        # round earlier, but only when Sleeper projections prove a tier cliff.
        required_position = "DEF" if state.round_number == 17 and counts["DEF"] < 1 else None
        if (
            state.round_number == 16
            and counts["DEF"] < 1
            and self._defense_pivot_is_warranted(state)
        ):
            required_position = "DEF"
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

    def _defense_pivot_is_warranted(self, state: DraftState) -> bool:
        names = state.sleeper_ranked_specialists.get("DEF", ())
        projections = state.sleeper_specialist_points.get("DEF", {})
        values = [self._named_points(projections, name) for name in names]
        return self.value_model.should_pivot("DEF", values, state)

    @staticmethod
    def _named_points(values: dict[str, float], player_name: str) -> float | None:
        expected = "".join(character for character in player_name.casefold() if character.isalnum())
        for name, points in values.items():
            normalized = "".join(character for character in name.casefold() if character.isalnum())
            if normalized == expected:
                return float(points)
        return None
