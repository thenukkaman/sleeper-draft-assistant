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
from .lookahead import PreparedPick
from .models import Recommendation
from .policies.base import DraftPolicy


class DraftRoom(Protocol):
    """Minimal UI boundary; any browser driver can implement this.

    ``draft`` must use the fresh, exact player-row action—not a stale coordinate.
    Its next ``observe`` call must wait for Sleeper to publish the result.
    """

    def observe(self) -> BrowserObservation: ...

    def draft(self, player_name: str) -> None: ...


class AutoPickControllableDraftRoom(DraftRoom, Protocol):
    """Optional capability for a room that can explicitly disable auto-pick."""

    def set_auto_pick(self, enabled: bool) -> None: ...


@dataclass(frozen=True)
class AutoPickRecovery:
    """Evidence from a single auto-pick remediation attempt.

    A recovered toggle is not the same as a recovered pick: Sleeper may already
    have advanced the clock while the UI was being updated.
    """

    attempted: bool
    recovered: bool
    pick_was_missed: bool
    before_pick_number: int | None
    after_pick_number: int | None
    reason: str


@dataclass(frozen=True)
class DraftAttempt:
    acted: bool
    confirmed: bool
    player_name: str | None
    recommendation: Recommendation
    gate: ExecutionGate
    reason: str
    auto_pick_recovery: AutoPickRecovery | None = None


class ClockFirstDraftDriver:
    """Read, decide, recheck, and click in one clock-sensitive operation."""

    def __init__(self, board: Board, policy: DraftPolicy, guard: AutonomousDraftGuard) -> None:
        self.board = board
        self.policy = policy
        self.guard = guard

    def run_once(self, room: DraftRoom, prepared: PreparedPick | None = None) -> DraftAttempt:
        first = room.observe()
        if first.blocker is not None:
            return self._blocked_by_browser(first, None)
        recovery = self._recover_auto_pick(room, first)
        if recovery is not None:
            if not recovery.recovered or recovery.pick_was_missed:
                state = room.observe().to_state()
                recommendation = self.policy.recommend(self.board, state)
                gate = self.guard.evaluate(state, recommendation)
                return DraftAttempt(
                    False,
                    False,
                    None,
                    recommendation,
                    gate,
                    recovery.reason,
                    recovery,
                )
            first = room.observe()
        recommendation = self._recommend(first.to_state(), prepared)
        gate = self.guard.evaluate(first.to_state(), recommendation)
        if not gate.allowed:
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                gate,
                "Initial clock/identity gate blocked action.",
                recovery,
            )

        # The UI may change in the milliseconds between recommendation and click.
        commit = room.observe()
        if commit.blocker is not None:
            return self._blocked_by_browser(commit, recovery)
        commit_recovery = self._recover_auto_pick(room, commit)
        if commit_recovery is not None:
            recovery = commit_recovery
            if not recovery.recovered or recovery.pick_was_missed:
                state = room.observe().to_state()
                recommendation = self._recommend(state, prepared)
                gate = self.guard.evaluate(state, recommendation)
                return DraftAttempt(False, False, None, recommendation, gate, recovery.reason, recovery)
            commit = room.observe()
            recommendation = self._recommend(commit.to_state(), prepared)
        commit_gate = self.guard.evaluate(commit.to_state(), recommendation)
        if not commit_gate.allowed:
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                commit_gate,
                "Draft state changed before commit.",
                recovery,
            )
        if (commit.pick_label, commit.current_pick_number) != (first.pick_label, first.current_pick_number):
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                commit_gate,
                "The clock advanced before commit.",
                recovery,
            )

        primary = recommendation.primary
        if primary is None:
            return DraftAttempt(False, False, None, recommendation, commit_gate, "No named player was returned.", recovery)
        if not commit.has_draft_action(primary.player.name):
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                commit_gate,
                "Sleeper did not expose an exact semantic DRAFT control for the recommended player; do not click a queue or details control.",
                recovery,
            )
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
                recovery,
            )
        if published.current_pick_number == commit.current_pick_number:
            return DraftAttempt(
                True,
                False,
                primary.player.name,
                recommendation,
                commit_gate,
                "Sleeper recorded the player but the draft clock did not advance; do not retry automatically.",
                recovery,
            )
        return DraftAttempt(
            True,
            True,
            primary.player.name,
            recommendation,
            commit_gate,
            "Draft click verified in Sleeper.",
            recovery,
        )

    def _recommend(self, state, prepared: PreparedPick | None) -> Recommendation:
        """Use a validated precomputed ladder when it still exactly applies."""

        if prepared is not None:
            recommendation = prepared.recommendation_for(state)
            if recommendation is not None:
                return recommendation
        return self.policy.recommend(self.board, state)

    def _blocked_by_browser(
        self,
        observation: BrowserObservation,
        recovery: AutoPickRecovery | None,
    ) -> DraftAttempt:
        """Fail closed before a modal, spinner, or stalled page can cost a pick.

        Browser recovery belongs to the executor, not the ranking policy. This
        transaction therefore records the policy's current recommendation for
        diagnosis but never toggles auto-pick or attempts a player action while
        a browser blocker is present.
        """

        recommendation = self.policy.recommend(self.board, observation.to_state())
        base_gate = self.guard.evaluate(observation.to_state(), recommendation)
        blocker_reason = observation.blocker.reason() if observation.blocker else "Browser blocker was not described."
        gate = ExecutionGate(False, base_gate.reasons + (blocker_reason,))
        return DraftAttempt(
            False,
            False,
            None,
            recommendation,
            gate,
            "Browser UI is blocked; do not submit, retry, or change auto-pick until the executor re-observes a clear state.",
            recovery,
        )

    @staticmethod
    def _recover_auto_pick(room: DraftRoom, observation: BrowserObservation) -> AutoPickRecovery | None:
        """Disable auto-pick first, then prove the same pick is still salvageable."""

        if observation.auto_pick_enabled is not True:
            return None
        toggle = getattr(room, "set_auto_pick", None)
        if not callable(toggle):
            return AutoPickRecovery(
                attempted=False,
                recovered=False,
                pick_was_missed=False,
                before_pick_number=observation.current_pick_number,
                after_pick_number=None,
                reason="Auto-pick is enabled but this draft-room adapter cannot disable it; action is blocked.",
            )
        toggle(False)
        after = room.observe()
        if after.auto_pick_enabled is not False:
            return AutoPickRecovery(
                attempted=True,
                recovered=False,
                pick_was_missed=False,
                before_pick_number=observation.current_pick_number,
                after_pick_number=after.current_pick_number,
                reason="Auto-pick remained enabled after the targeted disable action; action is blocked.",
            )
        if after.current_pick_number != observation.current_pick_number:
            return AutoPickRecovery(
                attempted=True,
                recovered=True,
                pick_was_missed=True,
                before_pick_number=observation.current_pick_number,
                after_pick_number=after.current_pick_number,
                reason="Auto-pick was disabled, but Sleeper had already advanced the clock; record the missed pick and do not retry.",
            )
        return AutoPickRecovery(
            attempted=True,
            recovered=True,
            pick_was_missed=False,
            before_pick_number=observation.current_pick_number,
            after_pick_number=after.current_pick_number,
            reason="Auto-pick was disabled and the same live pick remains salvageable.",
        )
