"""Policy-only next-pick preparation for a clock-sensitive browser runner."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .board import Board, normalize_name
from .models import Candidate, DraftState, Recommendation
from .policies.base import DraftPolicy


def pick_label_for_number(pick_number: int, teams: int = 12) -> tuple[int, str]:
    """Return the round and Sleeper snake label for an absolute pick number."""

    if pick_number < 1:
        raise ValueError("pick_number must be positive")
    if teams < 2:
        raise ValueError("teams must be at least two")
    round_number = (pick_number - 1) // teams + 1
    # Sleeper's cell label is the chronological pick *within the round*, not
    # the team column. In a snake draft, 2.08 is therefore absolute pick 20
    # even though that cell sits under the fifth team column.
    pick_in_round = (pick_number - 1) % teams + 1
    return round_number, f"{round_number}.{pick_in_round:02d}"


@dataclass(frozen=True)
class PreparedPick:
    """A next-pick candidate ladder computed before the clock reaches us.

    It is intentionally not an authorization to act. ``recommendation_for``
    returns ``None`` unless the final browser observation is the same planned
    pick, preserves our roster, and includes all previously known drafted
    players. Opponent selections are then removed from the cached ladder in
    constant time.
    """

    pick_number: int
    round_number: int
    pick_label: str
    roster_positions: tuple[str, ...]
    prepared_drafted: frozenset[str]
    policy_name: str
    directive: str
    warnings: tuple[str, ...]
    requires_special_teams_policy: bool
    candidates: tuple[Candidate, ...]

    def recommendation_for(self, state: DraftState) -> Recommendation | None:
        if (
            state.current_pick_number != self.pick_number
            or state.our_pick_number != self.pick_number
            or state.round_number != self.round_number
            or state.pick_label != self.pick_label
            or state.roster_positions != self.roster_positions
        ):
            return None

        drafted = {normalize_name(name) for name in state.drafted_players}
        if not {normalize_name(name) for name in self.prepared_drafted}.issubset(drafted):
            return None

        available = (
            {normalize_name(name) for name in state.available_players}
            if state.available_players is not None
            else None
        )
        candidates = tuple(
            candidate
            for candidate in self.candidates
            if normalize_name(candidate.player.name) not in drafted
            and (available is None or normalize_name(candidate.player.name) in available)
        )
        return Recommendation(
            policy_name=self.policy_name,
            candidates=candidates,
            directive=self.directive,
            warnings=self.warnings,
            requires_special_teams_policy=self.requires_special_teams_policy,
        )


class LookaheadPlanner:
    """Prepare the next approved team's ladder after every opponent pick."""

    def __init__(self, teams: int = 12) -> None:
        self.teams = teams

    def prepare_next(self, board: Board, policy: DraftPolicy, state: DraftState) -> PreparedPick | None:
        """Build a complete ladder for ``state.our_pick_number`` without UI I/O."""

        if state.our_pick_number is None:
            return None
        round_number, pick_label = pick_label_for_number(state.our_pick_number, self.teams)
        projected = replace(
            state,
            round_number=round_number,
            pick_label=pick_label,
            current_pick_number=state.our_pick_number,
        )
        recommendation = policy.recommend(board, projected, limit=len(board.players))
        return PreparedPick(
            pick_number=state.our_pick_number,
            round_number=round_number,
            pick_label=pick_label,
            roster_positions=state.roster_positions,
            prepared_drafted=state.drafted_players,
            policy_name=recommendation.policy_name,
            directive=recommendation.directive,
            warnings=recommendation.warnings,
            requires_special_teams_policy=recommendation.requires_special_teams_policy,
            candidates=recommendation.candidates,
        )
