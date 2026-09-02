"""Clock-first orchestration for a browser draft executor.

The browser adapter owns reading and clicking. The policy owns ranking. This
module joins the two for one guarded, non-blocking draft attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Callable, Protocol

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


AttemptEventRecorder = Callable[[str, datetime], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    final_observation: BrowserObservation | None = None
    # A transport error or an unverified submission permits continuous
    # observation, but forbids another player click for this same clock.
    quarantined: bool = False


class ClockFirstDraftDriver:
    """Read, decide, recheck, and click in one clock-sensitive operation."""

    def __init__(
        self,
        board: Board,
        policy: DraftPolicy,
        guard: AutonomousDraftGuard,
        now: Callable[[], datetime] = _utc_now,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        self.board = board
        self.policy = policy
        self.guard = guard
        self.now = now
        self.pause = pause

    def run_once(
        self,
        room: DraftRoom,
        prepared: PreparedPick | None = None,
        initial_observation: BrowserObservation | None = None,
        record_event: AttemptEventRecorder | None = None,
    ) -> DraftAttempt:
        """Attempt one guarded pick using a pre-read observation when supplied.

        The persistent runner passes its poll result as ``initial_observation``
        so the action path never spends a second DOM read before considering a
        live clock. Event timestamps intentionally sit at this interface
        boundary: they measure browser control without leaking it into policy.
        """

        def event(name: str) -> None:
            if record_event is not None:
                record_event(name, self.now())

        first = initial_observation or room.observe()
        event("initial_observation")
        if first.blocker is not None:
            return self._blocked_by_browser(first, None)
        recovery = self._recover_auto_pick(room, first, event)
        if recovery is not None:
            if not recovery.recovered or recovery.pick_was_missed:
                final_observation = room.observe()
                state = final_observation.to_state()
                recommendation = self._recommend(state, prepared)
                gate = self.guard.evaluate(state, recommendation)
                return DraftAttempt(
                    False,
                    False,
                    None,
                    recommendation,
                    gate,
                    recovery.reason,
                    recovery,
                    final_observation,
                )
            first = room.observe()
            event("post_recovery_observation")
        recommendation = self._recommend(first.to_state(), prepared)
        event("recommendation_ready")
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
                first,
            )

        # The UI may change in the milliseconds between recommendation and click.
        commit = room.observe()
        event("commit_observation")
        if commit.blocker is not None:
            return self._blocked_by_browser(commit, recovery)
        commit_recovery = self._recover_auto_pick(room, commit, event)
        if commit_recovery is not None:
            recovery = commit_recovery
            if not recovery.recovered or recovery.pick_was_missed:
                final_observation = room.observe()
                state = final_observation.to_state()
                recommendation = self._recommend(state, prepared)
                gate = self.guard.evaluate(state, recommendation)
                return DraftAttempt(
                    False,
                    False,
                    None,
                    recommendation,
                    gate,
                    recovery.reason,
                    recovery,
                    final_observation,
                )
            commit = room.observe()
            event("post_recovery_commit_observation")

        # Re-derive the decision from the just-read commit state.  Normally
        # this is a cheap filter of the prepared ladder.  If an opponent took
        # its primary during the short interval since preparation, the next
        # viable candidate becomes primary before we ask the browser to act.
        # Lookahead is therefore a latency optimization, never stale authority.
        recommendation = self._recommend(commit.to_state(), prepared)
        event("recommendation_ready")
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
                commit,
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
                commit,
            )

        primary = recommendation.primary
        if primary is None:
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                commit_gate,
                "No named player was returned.",
                recovery,
                commit,
            )
        if not commit.has_draft_action(primary.player.name):
            ensure = getattr(room, "ensure_draft_action", None)
            if callable(ensure):
                # A virtualized Sleeper table may require a name search before
                # its exact row-level DRAFT control exists in the DOM.  This is
                # a bounded, non-submitting UI preparation step.  Treat its
                # result like another commit observation, not an authorization.
                commit = ensure(primary.player.name)
                event("commit_observation")
                if commit.blocker is not None:
                    return self._blocked_by_browser(commit, recovery)
                recommendation = self._recommend(commit.to_state(), prepared)
                event("recommendation_ready")
                commit_gate = self.guard.evaluate(commit.to_state(), recommendation)
                if not commit_gate.allowed:
                    return DraftAttempt(
                        False,
                        False,
                        None,
                        recommendation,
                        commit_gate,
                        "Draft state changed while locating the named Sleeper player row.",
                        recovery,
                        commit,
                    )
                if (commit.pick_label, commit.current_pick_number) != (first.pick_label, first.current_pick_number):
                    return DraftAttempt(
                        False,
                        False,
                        None,
                        recommendation,
                        commit_gate,
                        "The clock advanced while locating the named Sleeper player row.",
                        recovery,
                        commit,
                    )
                primary = recommendation.primary
                if primary is None:
                    return DraftAttempt(
                        False,
                        False,
                        None,
                        recommendation,
                        commit_gate,
                        "No named player was returned after locating the Sleeper row.",
                        recovery,
                        commit,
                    )
        if primary is None or not commit.has_draft_action(primary.player.name):
            return DraftAttempt(
                False,
                False,
                None,
                recommendation,
                commit_gate,
                "Sleeper did not expose an exact semantic DRAFT control for the recommended player; do not click a queue or details control.",
                recovery,
                commit,
            )
        event("selection_requested")
        try:
            room.draft(primary.player.name)
        except Exception as error:
            # The exact-row transport may have dispatched an event before it
            # reports an error.  Re-observe once for evidence, then quarantine
            # this clock: monitor and auto-pick recovery may continue, but a
            # second player click would be unsafe.
            try:
                published = room.observe()
                event("published_observation")
            except Exception:
                published = commit
            return DraftAttempt(
                True,
                False,
                primary.player.name,
                recommendation,
                commit_gate,
                f"Exact Sleeper DRAFT control raised {type(error).__name__}; monitor this clock but do not retry the player action.",
                recovery,
                published,
                True,
            )
        published = room.observe()
        event("published_observation")
        recorded = {normalize_name(name) for name in published.drafted_players}
        # Sleeper accepts the exact action before its drafted-cell DOM updates.
        # Give that publish event a bounded 600-ms confirmation window. This is
        # observation only: no fallback player and no second click is allowed.
        for _ in range(6):
            if (
                normalize_name(primary.player.name) in recorded
                and published.current_pick_number != commit.current_pick_number
            ):
                break
            self.pause(0.1)
            published = room.observe()
            event("published_observation")
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
                published,
                True,
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
                published,
                True,
            )
        return DraftAttempt(
            True,
            True,
            primary.player.name,
            recommendation,
            commit_gate,
            "Draft click verified in Sleeper.",
            recovery,
            published,
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
            observation,
        )

    def _recover_auto_pick(
        self,
        room: DraftRoom,
        observation: BrowserObservation,
        event: Callable[[str], None],
    ) -> AutoPickRecovery | None:
        """Disable auto-pick first, then prove the same pick is still salvageable."""

        if observation.auto_pick_enabled is not True:
            return None
        event("auto_pick_detected")
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
        event("auto_pick_disable_requested")
        toggle(False)
        after = room.observe()
        event("auto_pick_disabled_observed")
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
