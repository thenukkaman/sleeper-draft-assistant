"""Clock-first orchestration for a browser draft executor.

The browser adapter owns reading and clicking. The policy owns ranking. This
module joins the two for one guarded, non-blocking draft attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .autonomy import AutonomousDraftGuard, ExecutionGate
from .board import Board, normalize_name
from .interfaces.browser_observation import BrowserObservation
from .models import Recommendation
from .policies.base import DraftPolicy


class DraftRoom(Protocol):
    """Minimal UI boundary; any browser driver can implement this.

    ``draft`` must use the fresh, exact player-row action—not a stale coordinate.
    Its next ``observe`` call must wait for Sleeper to publish the result.
    """

    def observe(self) -> BrowserObservation: ...

    def draft(self, player_name: str) -> None: ...


@dataclass(frozen=True)
class DraftAttempt:
    acted: bool
    confirmed: bool
    player_name: str | None
    recommendation: Recommendation
    gate: ExecutionGate
    reason: str


class ClockFirstDraftDriver:
    """Read, decide, recheck, and click in one clock-sensitive operation."""

    def __init__(self, board: Board, policy: DraftPolicy, guard: AutonomousDraftGuard) -> None:
        self.board = board
        self.policy = policy
        self.guard = guard

    def run_once(self, room: DraftRoom) -> DraftAttempt:
        first = room.observe()
        recommendation = self.policy.recommend(self.board, first.to_state())
        gate = self.guard.evaluate(first.to_state(), recommendation)
        if not gate.allowed:
            return DraftAttempt(False, False, None, recommendation, gate, "Initial clock/identity gate blocked action.")

        # The UI may change in the milliseconds between recommendation and click.
        commit = room.observe()
        commit_gate = self.guard.evaluate(commit.to_state(), recommendation)
        if not commit_gate.allowed:
            return DraftAttempt(False, False, None, recommendation, commit_gate, "Draft state changed before commit.")
        if (commit.pick_label, commit.current_pick_number) != (first.pick_label, first.current_pick_number):
            return DraftAttempt(False, False, None, recommendation, commit_gate, "The clock advanced before commit.")

        primary = recommendation.primary
        if primary is None:
            return DraftAttempt(False, False, None, recommendation, commit_gate, "No named player was returned.")
        room.draft(primary.player.name)
        published = room.observe()
        recorded = {normalize_name(name) for name in published.drafted_players}
        if normalize_name(primary.player.name) not in recorded:
            # An ambiguous click must never trigger an automatic second click.
            return DraftAttempt(
                True,
                False,
                primary.player.name,
                recommendation,
                commit_gate,
                "Draft click was submitted but Sleeper did not publish the named player; do not retry automatically.",
            )
        if published.current_pick_number == commit.current_pick_number:
            return DraftAttempt(
                True,
                False,
                primary.player.name,
                recommendation,
                commit_gate,
                "Sleeper recorded the player but the draft clock did not advance; do not retry automatically.",
            )
        return DraftAttempt(True, True, primary.player.name, recommendation, commit_gate, "Draft click verified in Sleeper.")
